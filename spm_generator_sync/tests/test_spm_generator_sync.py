import copy
import gzip
import json
import re
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import spm_generator_sync as sync
from speedtree_legacy_cluster_contract import (
    LEGACY_CLUSTER_MARKER_VALUES,
    RECEIPT_KIND,
    RECEIPT_VERSION,
    marker_receipt_path,
)


def extra_xml():
    return """<Extra>
<m_nOrderValue>1</m_nOrderValue>
<m_bSetBackgroundIconColor>false</m_bSetBackgroundIconColor>
<m_vecBackgroundIconColor_r>0</m_vecBackgroundIconColor_r>
<m_vecBackgroundIconColor_g>0</m_vecBackgroundIconColor_g>
<m_vecBackgroundIconColor_b>0</m_vecBackgroundIconColor_b>
<m_vecBackgroundIconColor_a>0</m_vecBackgroundIconColor_a>
</Extra>"""


def property_xml(name, value):
    return f"<Property><Name>{name}</Name><Value>{value}</Value></Property>"


def generator_xml(guid, name, generator_type, level, properties=()):
    return (
        f'<Generator Type="{generator_type}">'
        f"<Level>{level}</Level><Name>{name}</Name><GUID>{guid}</GUID>"
        f"<Hidden>false</Hidden>{extra_xml()}"
        f"<Properties>{''.join(properties)}</Properties></Generator>"
    )


def link_xml(guid, source, target, name):
    return (
        f"<Link><SourceGUID>{source}</SourceGUID><TargetGUID>{target}</TargetGUID>"
        f"<Name>{name}</Name><GUID>{guid}</GUID><Hidden>false</Hidden>"
        "<Extra /><Properties /></Link>"
    )


def spm_xml(generators, links):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SpeedTree Version="8" VersionString="10.1.0 ">'
        f"<Generators>{''.join(generators)}</Generators>"
        f"<Links>{''.join(links)}</Links>"
        "<Nodes /></SpeedTree>"
    )


def write_spm(path, text):
    path = Path(path)
    path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))


def rename_generators(text, names_by_guid):
    root = ET.fromstring(text)
    for generator in root.findall("./Generators/Generator"):
        name = names_by_guid.get(generator.findtext("GUID"))
        if name is not None:
            generator.find("Name").text = name
    return ET.tostring(root, encoding="unicode")


def make_master():
    generators = [
        generator_xml("tree", "Tree", "Tree", 0),
        generator_xml("leaf-base", "Leaf", "Base", 1, [
            property_xml("Random Seeds:Base", "111"),
            property_xml("Generation:First", "0.2"),
        ]),
        generator_xml("leaf-branch", "Branch 1", "Branch", 2, [
            property_xml("Random Seeds:Generation", "222"),
            property_xml("Generation:First", "0.25"),
            property_xml("Materials:Branch:0:Material", "MasterBark"),
        ]),
        generator_xml("leaf-mesh-a", "Leaf 1", "Leaf Mesh", 3, [
            property_xml("Leaves:Size", "2.0"),
            property_xml("Leaves:Type:0:Material", "MasterLeaf"),
        ]),
        generator_xml("leaf-mesh-b", "Leaf 2", "Leaf Mesh", 3, [
            property_xml("Leaves:Size", "3.0"),
            property_xml("Leaves:Type:0:Material", "MasterLeaf2"),
        ]),
        generator_xml("branch-base", "Branch", "Base", 1),
        generator_xml("branch-child", "Branch 2", "Branch", 2, [
            property_xml("Generation:Last", "0.7"),
        ]),
        generator_xml("end-base", "End", "Base", 1),
        generator_xml("end-child", "Branch 3", "Branch", 2),
        generator_xml("end-cap", "Cap 1", "Cap", 3),
    ]
    links = [
        link_xml("l0a", "tree", "leaf-base", "Tree->Leaf"),
        link_xml("l0b", "tree", "branch-base", "Tree->Branch"),
        link_xml("l0c", "tree", "end-base", "Tree->End"),
        link_xml("l1", "leaf-base", "leaf-branch", "Leaf->Branch"),
        link_xml("l2", "leaf-branch", "leaf-mesh-a", "Branch->Leaf 1"),
        link_xml("l3", "leaf-branch", "leaf-mesh-b", "Branch->Leaf 2"),
        link_xml("l4", "branch-base", "branch-child", "Branch->Branch"),
        link_xml("l5", "end-base", "end-child", "End->Branch"),
        link_xml("l6", "end-child", "end-cap", "Branch->Cap"),
    ]
    return spm_xml(generators, links)


def make_target():
    generators = [
        generator_xml("target-tree", "Tree", "Tree", 0),
        generator_xml("target-leaf-base", "Leaf 2", "Base", 1, [
            property_xml("Random Seeds:Base", "999"),
            property_xml("Generation:First", "0.9"),
        ]),
        generator_xml("target-leaf-branch", "Branch 50", "Branch", 2, [
            property_xml("Random Seeds:Generation", "888"),
            property_xml("Generation:First", "0.95"),
            property_xml("Materials:Branch:0:Material", "TargetBark"),
        ]),
        generator_xml("target-leaf-mesh", "Leaf 50", "Leaf Mesh", 3, [
            property_xml("Leaves:Size", "0.5"),
            property_xml("Leaves:Type:0:Material", "TargetLeaf"),
        ]),
        generator_xml("target-knot", "Knot unique", "Knot", 3, [
            property_xml("Knot:Size", "9"),
        ]),
        generator_xml("target-ref", "Leaf ref", "BaseRef", 2, [
            property_xml("Settings:Base filter", "Leaf 2"),
            property_xml("Generation:First", "0.1"),
        ]),
        generator_xml("target-branch-big", "BranchBig", "Base", 1),
        generator_xml("target-branch-child", "Branch 80", "Branch", 2, [
            property_xml("Generation:Last", "0.1"),
        ]),
        generator_xml("target-branch-small", "BranchSmall", "Base", 1),
        generator_xml("target-branch-child-2", "Branch 81", "Branch", 2, [
            property_xml("Generation:Last", "0.2"),
        ]),
        generator_xml("target-end", "End 2", "Base", 1),
        generator_xml("target-end-child", "Branch 90", "Branch", 2),
        generator_xml("target-end-cap", "Cap 90", "Cap", 3),
    ]
    links = [
        link_xml("tl0a", "target-tree", "target-leaf-base", "Tree->Leaf"),
        link_xml("tl0b", "target-tree", "target-branch-big", "Tree->BranchBig"),
        link_xml("tl0c", "target-tree", "target-branch-small", "Tree->BranchSmall"),
        link_xml("tl0d", "target-tree", "target-end", "Tree->End"),
        link_xml("tl1", "target-leaf-base", "target-leaf-branch", "Leaf->Branch"),
        link_xml("tl2", "target-leaf-branch", "target-leaf-mesh", "Branch->Leaf"),
        link_xml("tl3", "target-leaf-branch", "target-knot", "Branch->Knot"),
        link_xml("tl4", "target-tree", "target-ref", "Tree->Leaf ref"),
        link_xml("tl5", "target-branch-big", "target-branch-child", "BranchBig->Branch"),
        link_xml("tl6", "target-branch-small", "target-branch-child-2", "BranchSmall->Branch"),
        link_xml("tl7", "target-end", "target-end-child", "End->Branch"),
        link_xml("tl8", "target-end-child", "target-end-cap", "Branch->Cap"),
    ]
    return spm_xml(generators, links)


def make_target_without_end():
    root = ET.fromstring(make_target())
    removed = {"target-end", "target-end-child", "target-end-cap"}
    generators = root.find("Generators")
    links = root.find("Links")
    for generator in list(generators or []):
        if generator.findtext("GUID") in removed:
            generators.remove(generator)
    for link in list(links or []):
        if link.findtext("SourceGUID") in removed or link.findtext("TargetGUID") in removed:
            links.remove(link)
    return ET.tostring(root, encoding="unicode")


def make_target_without_knot():
    root = ET.fromstring(make_target())
    generators = root.find("Generators")
    links = root.find("Links")
    for generator in list(generators or []):
        if generator.findtext("GUID") == "target-knot":
            generators.remove(generator)
    for link in list(links or []):
        if (
            link.findtext("SourceGUID") == "target-knot"
            or link.findtext("TargetGUID") == "target-knot"
        ):
            links.remove(link)
    return ET.tostring(root, encoding="unicode")


def make_empty_target_without_links():
    return spm_xml(
        [generator_xml("empty-tree", "Tree", "Tree", 0)],
        [],
    ).replace("<Links></Links>", "")


def make_pass_master(base_pass=2):
    return spm_xml(
        [
            generator_xml("master-tree", "Tree", "Tree", 0),
            generator_xml("master-leaf-base", "Leaf", "Base", 1, [
                property_xml(sync.GENERATION_PASS_PROPERTY, str(base_pass)),
                property_xml("Generation:First", "0.25"),
            ]),
            # Base is a reusable template boundary, so its child may remain in
            # pass 1 even when the Base itself is consumed in a later pass.
            generator_xml("master-leaf-child", "Leaf branch", "Branch", 2, [
                property_xml(sync.GENERATION_PASS_PROPERTY, "1"),
            ]),
        ],
        [
            link_xml("master-tree-base", "master-tree", "master-leaf-base", "Tree->Leaf"),
            link_xml(
                "master-base-child", "master-leaf-base", "master-leaf-child",
                "Leaf->Leaf branch",
            ),
        ],
    )


def make_pass_target(base_pass=2, ref_pass=2, parent_pass=3, base_filter="Leaf"):
    return spm_xml(
        [
            generator_xml("target-tree", "Tree", "Tree", 0),
            generator_xml("target-parent", "Reference parent", "Branch", 1, [
                property_xml(sync.GENERATION_PASS_PROPERTY, str(parent_pass)),
            ]),
            generator_xml("target-ref", "Leaf reference", "BaseRef", 2, [
                property_xml(sync.GENERATION_PASS_PROPERTY, str(ref_pass)),
                property_xml("Settings:Base filter", base_filter),
            ]),
            generator_xml("target-leaf-base", "Leaf", "Base", 1, [
                property_xml(sync.GENERATION_PASS_PROPERTY, str(base_pass)),
                property_xml("Generation:First", "0.9"),
            ]),
            generator_xml("target-leaf-child", "Leaf branch target", "Branch", 2, [
                property_xml(sync.GENERATION_PASS_PROPERTY, "1"),
            ]),
        ],
        [
            link_xml("target-tree-parent", "target-tree", "target-parent", "Tree->Parent"),
            link_xml("target-parent-ref", "target-parent", "target-ref", "Parent->Reference"),
            link_xml("target-tree-base", "target-tree", "target-leaf-base", "Tree->Leaf"),
            link_xml(
                "target-base-child", "target-leaf-base", "target-leaf-child",
                "Leaf->Leaf branch target",
            ),
        ],
    )


def property_value(generator, name):
    properties = generator.find("Properties")
    for prop in list(properties or []):
        if prop.findtext("Name") == name:
            return prop.findtext("Value")
    return None


def with_tree_radius(text, radius):
    root = ET.fromstring(text)
    tree = next(
        item for item in root.find("Generators")
        if item.attrib.get("Type") == "Tree"
    )
    tree.find("Properties").append(
        ET.fromstring(property_xml("Shape:Radius", str(radius)))
    )
    return ET.tostring(root, encoding="unicode")


def with_assets(text, assets):
    section = "<Assets>" + "".join(
        f'<{kind} ID="{asset_id}" Name="{name}" />'
        for kind, asset_id, name in assets
    ) + "</Assets>"
    return text.replace("<Generators>", section + "<Generators>", 1)


class GeneratorSyncTests(unittest.TestCase):
    def test_scan_nests_cluster_normalized_blends_under_owner_not_as_spm_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = root / "Tree_elm"
            cluster = owner / "Cluster"
            cluster.mkdir(parents=True)
            write_spm(owner / "SK_Tree_elm_01.spm", make_master())
            write_spm(cluster / "branch_elm_01.spm", make_target())
            write_spm(cluster / "SK_branch_elm_01.spm", make_target())
            (cluster / "SK_branch_elm_01.blend").touch()

            board = sync.scan_tree_folders(root, sk_only=True)

            self.assertEqual([Path(row["folder"]) for row in board], [owner])
            self.assertEqual(board[0]["spms"], ["SK_Tree_elm_01.spm"])
            self.assertEqual(
                board[0]["cluster_blends"][0]["blend"],
                str((cluster / "SK_branch_elm_01.blend").absolute()),
            )
            self.assertEqual(
                board[0]["cluster_blends"][0]["folder_relation"],
                "off",
            )

    def test_quick_scan_defers_physical_cluster_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = root / "Tree_elm"
            owner.mkdir()
            write_spm(owner / "SK_Tree_elm_01.spm", make_master())

            with mock.patch.object(
                sync,
                "discover_cluster_blend_relations",
                return_value=[],
            ) as discover:
                board = sync.scan_tree_folders(
                    root,
                    sk_only=True,
                    verify_physical=False,
                )

            self.assertEqual([Path(row["folder"]) for row in board], [owner])
            discover.assert_any_call(owner, verify_physical=False)

    def test_scan_reports_folder_progress_without_changing_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = root / "Tree_elm"
            owner.mkdir()
            write_spm(owner / "SK_Tree_elm_01.spm", make_master())
            progress = []

            board = sync.scan_tree_folders(
                root,
                sk_only=True,
                verify_physical=False,
                progress_callback=lambda stage, percent: progress.append(
                    (stage, percent)
                ),
            )

            self.assertEqual([Path(row["folder"]) for row in board], [owner])
            self.assertTrue(progress)
            self.assertEqual(progress[-1][1], 100)
            self.assertIn("2/2", progress[-1][0])
            self.assertTrue(any("Tree_elm" in stage for stage, _ in progress))

    def test_cloned_and_previously_wrong_leaf_material_ids_are_remapped_by_asset_name(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            master_text = with_assets(
                make_master().replace("MasterLeaf2", "12"),
                [("Material_v8", "12", "M_Leaf_black_locast_01")],
            )
            target_text = with_assets(
                make_target(),
                [
                    ("Material_v8", "12", "M_cluster_black_locast_01"),
                    ("Material_v8", "14", "M_Leaf_black_locast_01"),
                ],
            )
            write_spm(master, master_text)
            write_spm(target, target_text)
            mapping = {
                "Leaf 2": "Leaf", "BranchBig": "Branch",
                "BranchSmall": None, "End 2": "End",
            }

            first = sync.build_sync_plan(master, target, mapping)
            added_leaf = next(
                detail for result in first.base_results
                for detail in result.added_node_details
                if detail["name"] == "Leaf 2"
            )
            first_doc = sync.SPMDocument(
                target, first.patched_text, first.compressed, full=True
            )
            added_generator = first_doc.by_guid[added_leaf["guid"]]
            self.assertEqual(
                property_value(added_generator, "Leaves:Type:0:Material"), "14"
            )

            # Recreate the legacy bug: the cloned leaf contains master-local ID
            # 12, which means cluster material in the follower.
            material_prop = next(
                prop for prop in added_generator.findall("./Properties/Property")
                if prop.findtext("Name") == "Leaves:Type:0:Material"
            )
            material_prop.find("Value").text = "12"
            write_spm(target, first_doc.render())

            repair = sync.build_sync_plan(master, target, mapping)
            repaired_doc = sync.SPMDocument(
                target, repair.patched_text, repair.compressed, full=True
            )
            repaired_generator = repaired_doc.by_guid[added_leaf["guid"]]
            self.assertEqual(
                property_value(repaired_generator, "Leaves:Type:0:Material"), "14"
            )
            self.assertGreaterEqual(
                sum(item.asset_reference_updates for item in repair.base_results), 1
            )

    def test_matched_generator_asset_selection_follows_master_by_exact_name(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"

            master_root = ET.fromstring(with_assets(
                make_master().replace("MasterLeaf", "11", 1),
                [
                    ("Material_v8", "11", "M_leaf_dogwood_02"),
                    ("Mesh", "31", "cluster_dogwood_02"),
                ],
            ))
            master_leaf = next(
                item for item in master_root.findall("./Generators/Generator")
                if item.findtext("GUID") == "leaf-mesh-a"
            )
            master_leaf.find("Properties").append(
                ET.fromstring(property_xml("Leaves:Type:0:Mesh", "31"))
            )

            target_root = ET.fromstring(with_assets(
                make_target().replace("TargetLeaf", "22", 1),
                [
                    ("Material_v8", "21", "M_leaf_dogwood_02"),
                    ("Material_v8", "22", "M_leaf_dogwood_04"),
                    ("Mesh", "41", "cluster_dogwood_02"),
                    ("Mesh", "42", "cluster_dogwood_04"),
                ],
            ))
            target_leaf = next(
                item for item in target_root.findall("./Generators/Generator")
                if item.findtext("GUID") == "target-leaf-mesh"
            )
            target_leaf.find("Properties").append(
                ET.fromstring(property_xml("Leaves:Type:0:Mesh", "42"))
            )

            write_spm(master, ET.tostring(master_root, encoding="unicode"))
            write_spm(target, ET.tostring(target_root, encoding="unicode"))
            mapping = {
                "Leaf 2": "Leaf", "BranchBig": "Branch",
                "BranchSmall": None, "End 2": "End",
            }

            plan = sync.build_sync_plan(master, target, mapping)
            patched = sync.SPMDocument(
                target, plan.patched_text, plan.compressed, full=True
            )
            patched_leaf = patched.by_guid["target-leaf-mesh"]
            self.assertEqual(
                property_value(patched_leaf, "Leaves:Type:0:Material"),
                "21",
            )
            self.assertEqual(
                property_value(patched_leaf, "Leaves:Type:0:Mesh"),
                "41",
            )
            self.assertEqual(
                sum(item.asset_reference_updates for item in plan.base_results),
                2,
            )
            self.assertFalse(any(result.copied_assets for result in plan.base_results))

            write_spm(target, plan.patched_text)
            repeat = sync.build_sync_plan(master, target, mapping)
            self.assertFalse(repeat.changed)

    def test_missing_material_and_cutout_meshes_are_copied_with_new_local_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            master_assets = (
                '<Assets><Material_v8 ID="12" Name="M_MasterLeaf">'
                '<CutoutMeshID>18</CutoutMeshID><BackMaterialID>-1</BackMaterialID>'
                '<SupplementalCutoutMeshIDs Count="1"><CutoutMesh ID="19" />'
                '</SupplementalCutoutMeshIDs></Material_v8>'
                '<Mesh ID="18" Name="MasterLeaf Cutout" />'
                '<Mesh ID="19" Name="MasterLeaf Cutout 2" /></Assets>'
            )
            master_text = make_master().replace("MasterLeaf2", "12")
            master_text = master_text.replace("<Generators>", master_assets + "<Generators>", 1)
            target_text = with_assets(
                make_target(),
                [
                    ("Material_v8", "12", "M_cluster_target"),
                    ("Mesh", "18", "Existing target cutout"),
                ],
            )
            write_spm(master, master_text)
            write_spm(target, target_text)
            mapping = {
                "Leaf 2": "Leaf", "BranchBig": "Branch",
                "BranchSmall": None, "End 2": "End",
            }

            plan = sync.build_sync_plan(master, target, mapping)
            patched = sync.SPMDocument(
                target, plan.patched_text, plan.compressed, full=True
            )
            material_id = patched.asset_id("Material_v8", "M_MasterLeaf")
            cutout_id = patched.asset_id("Mesh", "MasterLeaf Cutout")
            supplemental_id = patched.asset_id("Mesh", "MasterLeaf Cutout 2")
            self.assertIsNotNone(material_id)
            self.assertIsNotNone(cutout_id)
            self.assertIsNotNone(supplemental_id)
            self.assertNotEqual(material_id, "12")
            self.assertNotEqual(cutout_id, "18")

            added_leaf = next(
                detail for result in plan.base_results
                for detail in result.added_node_details
                if detail["name"] == "Leaf 2"
            )
            added_generator = patched.by_guid[added_leaf["guid"]]
            self.assertEqual(
                property_value(added_generator, "Leaves:Type:0:Material"), material_id
            )
            material = patched.asset_elements_by_id["Material_v8"][material_id]
            self.assertEqual(material.findtext("CutoutMeshID"), cutout_id)
            self.assertEqual(
                material.find("./SupplementalCutoutMeshIDs/CutoutMesh").attrib["ID"],
                supplemental_id,
            )
            copied = [
                (asset["kind"], asset["name"])
                for result in plan.base_results for asset in result.copied_assets
            ]
            self.assertEqual(
                copied,
                [
                    ("Material_v8", "M_MasterLeaf"),
                    ("Mesh", "MasterLeaf Cutout"),
                    ("Mesh", "MasterLeaf Cutout 2"),
                ],
            )

            write_spm(target, plan.patched_text)
            repeat = sync.build_sync_plan(master, target, mapping)
            self.assertFalse(repeat.changed)
            self.assertFalse(any(result.copied_assets for result in repeat.base_results))

    def test_cloned_generator_uses_atlas_authored_binding_not_managed_output(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"

            master_root = ET.fromstring(make_master())
            source_generator = next(
                item
                for item in master_root.findall("./Generators/Generator")
                if item.findtext("GUID") == "leaf-mesh-b"
            )
            source_properties = source_generator.find("Properties")
            source_properties.append(
                ET.fromstring(
                    "<Property><Name>Leaves:Type</Name>"
                    "<MultiPropertyChildren>2</MultiPropertyChildren>"
                    "</Property>"
                )
            )
            source_properties.append(
                ET.fromstring(
                    property_xml("Leaves:Type:0:Mesh", "20")
                )
            )
            source_properties.append(
                ET.fromstring(
                    property_xml("Leaves:Type:1:Material", "12")
                )
            )
            source_properties.append(
                ET.fromstring(
                    property_xml("Leaves:Type:1:Mesh", "21")
                )
            )
            source_properties.append(
                ET.fromstring(
                    property_xml("Leaves:Type:1:Weight", "1")
                )
            )
            source_material = (
                '<Assets><Material_v8 ID="12" Name="M_cluster_test">'
                "<CutoutMeshID>20</CutoutMeshID>"
                '<SupplementalCutoutMeshIDs Count="1">'
                '<CutoutMesh ID="21" />'
                "</SupplementalCutoutMeshIDs></Material_v8>"
                '<Mesh ID="18" Name="authored_01" />'
                '<Mesh ID="19" Name="authored_02" />'
                '<Mesh ID="20" Name="managed_01" />'
                '<Mesh ID="21" Name="managed_02" /></Assets>'
            )
            master_text = ET.tostring(master_root, encoding="unicode")
            master_text = master_text.replace(
                "<Generators>",
                source_material + "<Generators>",
                1,
            )

            target_material = (
                '<Assets><Material_v8 ID="14" Name="M_cluster_test">'
                "<CutoutMeshID>30</CutoutMeshID>"
                '<SupplementalCutoutMeshIDs Count="1">'
                '<CutoutMesh ID="31" />'
                "</SupplementalCutoutMeshIDs></Material_v8>"
                '<Mesh ID="30" Name="target_authored_01" />'
                '<Mesh ID="31" Name="target_authored_02" /></Assets>'
            )
            target_text = make_target().replace(
                "<Generators>",
                target_material + "<Generators>",
                1,
            )
            write_spm(master, master_text)
            write_spm(target, target_text)

            manifest = {
                "spm": str(master),
                "source_material_adoption": {
                    "material_id": 12,
                    "original_mesh_ids": [18, 19],
                },
                "generator_connection": {
                    "complete": True,
                    "bindings": [
                        {
                            "generator_guid": "leaf-mesh-b",
                            "generator_name": "Leaf 2",
                            "slot_prefix": "Leaves:Type:0",
                            "source_material_id": 12,
                            "source_material_name": "M_cluster_test",
                            "source_mesh_id": 18,
                            "target_material_id": 12,
                            "target_mesh_id": 20,
                            "created_slot": False,
                        },
                        {
                            "generator_guid": "leaf-mesh-b",
                            "generator_name": "Leaf 2",
                            "slot_prefix": "Leaves:Type:1",
                            "source_material_id": 12,
                            "source_material_name": "M_cluster_test",
                            "source_mesh_id": 19,
                            "target_material_id": 12,
                            "target_mesh_id": 21,
                            "created_slot": True,
                            "variant_parent_property": "Leaves:Type",
                            "variant_parent_children_before": 1,
                            "created_property_names": [
                                "Leaves:Type:1:Material",
                                "Leaves:Type:1:Mesh",
                                "Leaves:Type:1:Weight",
                            ],
                        },
                    ],
                },
            }
            manifest_dir = folder / ".atlas_leaf_speedtree_targets"
            manifest_dir.mkdir()
            (manifest_dir / "tree_01.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
                "End 2": "End",
            }

            plan = sync.build_sync_plan(master, target, mapping)
            patched = sync.SPMDocument(
                target,
                plan.patched_text,
                plan.compressed,
                full=True,
            )
            added_leaf = next(
                detail
                for result in plan.base_results
                for detail in result.added_node_details
                if detail["name"] == "Leaf 2"
            )
            clone = patched.by_guid[added_leaf["guid"]]

            self.assertEqual(
                property_value(clone, "Leaves:Type:0:Material"),
                "14",
            )
            self.assertEqual(
                property_value(clone, "Leaves:Type:0:Mesh"),
                "30",
            )
            self.assertIsNone(
                property_value(clone, "Leaves:Type:1:Material")
            )
            self.assertIsNone(
                property_value(clone, "Leaves:Type:1:Mesh")
            )
            parent = next(
                prop
                for prop in clone.findall("./Properties/Property")
                if prop.findtext("Name") == "Leaves:Type"
            )
            self.assertEqual(
                parent.findtext("MultiPropertyChildren"),
                "1",
            )
            copied_names = {
                item["name"]
                for result in plan.base_results
                for item in result.copied_assets
            }
            self.assertNotIn("managed_01", copied_names)
            self.assertNotIn("managed_02", copied_names)

    def test_scale_risk_uses_tree_radius_and_blocks_dangerous_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_03.spm"
            write_spm(master, with_tree_radius(make_master(), 1.74))
            write_spm(target, with_tree_radius(make_master(), 10))

            source = sync.SPMDocument.from_path(master, full=False)
            follower = sync.SPMDocument.from_path(target, full=False)
            risk = sync.assess_scale_risk(source, follower)
            self.assertEqual(risk["level"], "blocked")
            self.assertAlmostEqual(risk["ratio"], 10 / 1.74)

            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = sync.source_base_categories(source)
            mapping = sync.suggest_base_map(source, follower, group["base_categories"])
            sync.assign_follower(manifest, master.name, target.name, mapping, confirmed=True)
            sync.save_manifest(folder, manifest)
            originals = {master: master.read_bytes(), target: target.read_bytes()}

            with self.assertRaisesRegex(sync.SyncError, "폭증 위험"):
                sync.apply_group_transaction(
                    folder, master.name, verify_speedtree=False
                )
            self.assertEqual(master.read_bytes(), originals[master])
            self.assertEqual(target.read_bytes(), originals[target])
            self.assertFalse((folder / sync.BACKUP_SUBDIR).exists())

    def test_batch_scale_skip_keeps_safe_sibling_sync_running(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            safe = folder / "tree_02.spm"
            blocked = folder / "tree_03.spm"
            write_spm(master, with_tree_radius(make_master(), 2))
            write_spm(safe, with_tree_radius(make_target(), 3))
            write_spm(blocked, with_tree_radius(make_target(), 10))

            source = sync.SPMDocument.from_path(master, full=False)
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = sync.source_base_categories(source)
            for target in (safe, blocked):
                follower = sync.SPMDocument.from_path(target, full=False)
                mapping = sync.suggest_base_map(
                    source,
                    follower,
                    group["base_categories"],
                )
                sync.assign_follower(
                    manifest,
                    master.name,
                    target.name,
                    mapping,
                    confirmed=True,
                )
            sync.save_manifest(folder, manifest)
            blocked_before = blocked.read_bytes()

            result = sync.apply_group_transaction(
                folder,
                master.name,
                verify_speedtree=False,
                skip_blocked_scale=True,
            )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(
                [Path(entry["target"]).name for entry in result["scale_skipped"]],
                [blocked.name],
            )
            self.assertEqual(blocked.read_bytes(), blocked_before)
            self.assertIn(str(safe), result["changed_files"])
            saved = sync.load_manifest(folder)
            saved_group = sync.find_group(saved, master.name)
            followers = {
                entry["file"]: entry
                for entry in saved_group["followers"]
            }
            self.assertIsNotNone(followers[safe.name]["last_sync"])
            self.assertIsNone(followers[blocked.name]["last_sync"])

    def test_speedtree_preflight_uses_temp_copy_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            original = folder / "tree_02.spm"
            write_spm(original, make_target())
            original_bytes = original.read_bytes()
            patched = make_target().replace("Knot unique", "Knot changed")
            seen = []

            def fail(temporary):
                temporary = Path(temporary)
                seen.append(temporary)
                self.assertNotEqual(temporary, original)
                self.assertTrue(temporary.name.startswith("__spm_sync_preflight_"))
                raise sync.SyncError("compute failed")

            with self.assertRaisesRegex(sync.SyncError, "원본 미수정"):
                sync.verify_temporary_patches(
                    [(original, patched, True)], fail
                )
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertTrue(seen)
            self.assertFalse(seen[0].exists())

    def test_speedtree_verify_rejects_texture_writing_before_process(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            spm = folder / "tree_02.spm"
            executable = folder / "SpeedTree_Modeler.exe"
            options = folder / "Options_HI_Xml.ini"
            write_spm(spm, make_target())
            executable.write_bytes(b"fake-speedtree")
            options.write_text(
                "[Options]\n"
                "Filetype=SpeedTree XML (*.xml)\n"
                "TextureSkipWriting=false\n",
                encoding="utf-8",
            )
            spm_before = spm.read_bytes()
            options_before = options.read_bytes()

            with mock.patch.object(sync, "run_streaming_process") as run:
                with self.assertRaisesRegex(
                    RuntimeError, "TextureSkipWriting=false"
                ):
                    sync.verify_speedtree_export(
                        spm, executable, options
                    )

            run.assert_not_called()
            self.assertEqual(spm.read_bytes(), spm_before)
            self.assertEqual(options.read_bytes(), options_before)

    def test_speedtree_verify_streams_stderr_and_reports_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            script = folder / "failure.spm"
            options = folder / "Options_HI_Xml.ini"
            script.write_text(
                "import sys\n"
                "print('starting', flush=True)\n"
                "print('fatal detail', file=sys.stderr, flush=True)\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            options.write_text(
                "[Options]\nTextureSkipWriting=true\n",
                encoding="utf-8",
            )
            output = []

            with self.assertRaisesRegex(
                sync.SyncError, "code 7.*fatal detail"
            ):
                sync.verify_speedtree_export(
                    script,
                    Path(sys.executable),
                    options,
                    output_callback=lambda channel, line: output.append(
                        (channel, line)
                    ),
                )

            self.assertIn(("stdout", "starting"), output)
            self.assertIn(("stderr", "fatal detail"), output)

    def test_auto_copy_verification_keeps_follower_extra_out_of_master_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            originals = {
                master: master.read_bytes(),
                target: target.read_bytes(),
            }

            verified_documents = {}

            def capture_verified_document(path, *_args):
                path = Path(path)
                verified_documents[path.name] = sync.SPMDocument.from_path(
                    path, full=True
                )

            with mock.patch.object(
                sync,
                "verify_speedtree_export",
                side_effect=capture_verified_document,
            ) as verify:
                plans = sync.verify_auto_copies(
                    folder,
                    master.name,
                    [target.name],
                    Path("SpeedTree.exe"),
                    Path("Options.ini"),
                )

            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].master_sync_nodes, 0)
            self.assertEqual(verify.call_count, 2)
            verified_master = next(
                document
                for name, document in verified_documents.items()
                if name.endswith(master.name)
            )
            verified_target = next(
                document
                for name, document in verified_documents.items()
                if name.endswith(target.name)
            )
            self.assertFalse(any(
                verified_master.generator_name(item) == "Knot unique"
                for item in verified_master.generators
            ))
            self.assertNotIn("target-knot", verified_target.by_guid)
            self.assertEqual(
                sum(
                    result.removed_nodes
                    for result in plans[0].base_results
                ),
                1,
            )
            self.assertEqual(master.read_bytes(), originals[master])
            self.assertEqual(target.read_bytes(), originals[target])
            self.assertFalse(any(
                path.name.startswith("__spm_sync_verify_")
                for path in folder.iterdir()
            ))

    def test_scan_only_suggests_master_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "tree"
            folder.mkdir()
            for name in ("tree_01.spm", "tree_02.spm", "tree_04.spm"):
                write_spm(folder / name, make_master())
            report = sync.scan_tree_folders(Path(temp))
            self.assertEqual(len(report), 1)
            self.assertEqual(report[0]["master_candidates"], ["tree_01.spm"])
            self.assertEqual(report[0]["relations"], {})

    def test_scan_can_filter_to_sk_named_spms(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "tree"
            folder.mkdir()
            for name in ("SK_tree_01.spm", "tree_02.spm"):
                write_spm(folder / name, make_master())
            report = sync.scan_tree_folders(Path(temp), sk_only=True)
            self.assertEqual(report[0]["spms"], ["SK_tree_01.spm"])
            self.assertEqual(report[0]["master_candidates"], ["SK_tree_01.spm"])

    def test_follower_or_independent_can_be_promoted_to_master(self):
        manifest = sync.default_manifest()
        sync.set_master(manifest, "tree_01.spm")
        sync.assign_follower(
            manifest, "tree_01.spm", "tree_03.spm", {"Leaf": "Leaf"}, confirmed=True
        )
        sync.set_independent(manifest, ["tree_04.spm"])

        sync.set_master(manifest, "tree_03.spm")
        relations = sync.relation_index(manifest)
        self.assertEqual(relations["tree_03.spm"]["role"], "master")
        original_group = sync.find_group(manifest, "tree_01.spm")
        self.assertFalse(any(
            item.get("file") == "tree_03.spm" for item in original_group["followers"]
        ))

        sync.set_master(manifest, "tree_04.spm")
        relations = sync.relation_index(manifest)
        self.assertEqual(relations["tree_04.spm"]["role"], "master")
        self.assertNotIn("tree_04.spm", manifest["independent"])

    def test_master_promotion_applies_base_mapping_colors_without_followers(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            write_spm(master, make_master())

            result = sync.promote_master(folder, master.name)

            manifest = sync.load_manifest(folder)
            group = sync.find_group(manifest, master.name)
            self.assertFalse(group["followers"])
            self.assertEqual(group["base_categories"]["Leaf"], "leaf")
            self.assertEqual(group["base_categories"]["Branch"], "branch")
            self.assertGreater(result["color_updates"], 0)
            self.assertTrue(result["changed"])
            self.assertTrue(Path(result["backup_dir"]).is_dir())

            document = sync.SPMDocument.from_path(master, full=True)
            for base in document.base_nodes():
                category = group["base_categories"][document.generator_name(base)]
                self.assertIn(category, sync.CATEGORY_COLORS)
                self.assertEqual(
                    base.findtext("Extra/m_bSetBackgroundIconColor"),
                    "true",
                )

    def test_master_promotion_rolls_back_spm_and_manifest_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            write_spm(master, make_master())
            original = master.read_bytes()

            def fail(_path):
                raise RuntimeError("verification failed")

            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                sync.promote_master(folder, master.name, verify_callback=fail)

            self.assertEqual(master.read_bytes(), original)
            self.assertFalse((folder / sync.MANIFEST_NAME).exists())

    def test_master_promotion_does_not_overwrite_concurrent_spm_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            write_spm(master, make_master())
            concurrent = make_master().replace(
                property_xml("Generation:First", "0.2"),
                property_xml("Generation:First", "0.37"),
                1,
            )
            original_standardize = sync.standardize_master_document

            def standardize_then_change(*args, **kwargs):
                result = original_standardize(*args, **kwargs)
                write_spm(master, concurrent)
                return result

            with mock.patch.object(
                sync,
                "standardize_master_document",
                side_effect=standardize_then_change,
            ):
                with self.assertRaises(sync.SyncError):
                    sync.promote_master(folder, master.name)

            self.assertEqual(sync.read_spm_text(master)[0], concurrent)
            self.assertFalse((folder / sync.BACKUP_SUBDIR).exists())
            self.assertFalse((folder / sync.MANIFEST_NAME).exists())

    def test_base_mapping_suggestion_supports_one_to_many_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.spm"
            target = Path(temp) / "target.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            source_doc = sync.SPMDocument.from_path(master, full=False)
            target_doc = sync.SPMDocument.from_path(target, full=False)
            mapping = sync.suggest_base_map(source_doc, target_doc)
            self.assertEqual(mapping["Leaf 2"], "Leaf")
            self.assertEqual(mapping["BranchBig"], "Branch")
            self.assertEqual(mapping["BranchSmall"], "Branch")
            self.assertEqual(mapping["End 2"], "End")

            plan = sync.build_sync_plan(master, target, mapping)
            self.assertFalse(plan.mapping_required)

    def test_custom_base_names_sync_without_optional_color_classification(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.spm"
            target = Path(temp) / "target.spm"
            write_spm(master, rename_generators(make_master(), {
                "leaf-base": "Frond Base 3",
                "branch-base": "Cluster Base 3",
                "end-base": "extend",
            }))
            target_text = rename_generators(make_target(), {
                "target-leaf-base": "Frond Base 4",
                "target-branch-big": "Cluster Base 4",
                "target-end": "extend 2",
            }).replace(
                property_xml("Settings:Base filter", "Leaf 2"),
                property_xml("Settings:Base filter", "Frond Base 4"),
            )
            write_spm(target, target_text)

            self.assertIsNone(sync.classify_base_name("Frond Base 3"))
            self.assertIsNone(sync.classify_base_name("Cluster Base 3"))
            self.assertIsNone(sync.classify_base_name("extend"))
            plan = sync.build_sync_plan(master, target, {
                "Frond Base 4": "Frond Base 3",
                "Cluster Base 4": "Cluster Base 3",
                "BranchSmall": None,
                "extend 2": "extend",
            })

            self.assertFalse(plan.mapping_required)
            self.assertEqual(
                {item.source_base for item in plan.base_results},
                {"Frond Base 3", "Cluster Base 3", "extend"},
            )
            self.assertTrue(all(
                item.category is None for item in plan.base_results
            ))

    def test_custom_unmapped_master_bases_are_still_additive(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.spm"
            target = Path(temp) / "target.spm"
            write_spm(master, rename_generators(make_master(), {
                "leaf-base": "Frond Base 3",
                "branch-base": "Cluster Base 3",
                "end-base": "extend",
            }))
            write_spm(target, make_empty_target_without_links())

            plan = sync.build_sync_plan(master, target, {})

            self.assertFalse(plan.mapping_required)
            self.assertEqual(
                plan.added_base_mappings,
                {
                    "Frond Base 3": "Frond Base 3",
                    "Cluster Base 3": "Cluster Base 3",
                    "extend": "extend",
                },
            )

    def test_base_ref_names_are_base_scoped_unique_export_safe_and_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "target.spm"
            root = ET.fromstring(make_target())
            generators = root.find("Generators")
            links = root.find("Links")
            existing_ref = next(
                item for item in generators
                if item.findtext("GUID") == "target-ref"
            )
            existing_ref.find("Name").text = "messy duplicate"
            duplicate_ref = copy.deepcopy(existing_ref)
            duplicate_ref.find("GUID").text = "target-ref-2"
            generators.append(duplicate_ref)
            links.append(ET.fromstring(
                link_xml("tl-ref-2", "target-tree", "target-ref-2", "Tree->messy duplicate")
            ))
            # A non-reference already owns the first desired export name.
            next(
                item for item in generators
                if item.findtext("GUID") == "target-leaf-branch"
            ).find("Name").text = "Ref_Leaf_2_001"
            write_spm(path, ET.tostring(root, encoding="unicode"))

            document = sync.SPMDocument.from_path(path, full=True)
            base_name_before = document.generator_name(document.resolve_base("Leaf 2"))
            filter_before = document.base_ref_filter(existing_ref)
            renames = sync.standardize_base_ref_names(document)
            names = sorted(document.generator_name(ref) for ref in document.base_refs())
            self.assertIn("Ref_Leaf_2_002", names)
            self.assertIn("Ref_Leaf_2_003", names)
            self.assertEqual(len(names), len({name.casefold() for name in names}))
            self.assertTrue(all(
                re.fullmatch(r"[A-Za-z0-9_]+", name) for name in names
            ))
            self.assertEqual(
                document.generator_name(document.resolve_base("Leaf 2")),
                base_name_before,
            )
            self.assertEqual(document.base_ref_filter(document.by_guid["target-ref"]), filter_before)
            self.assertTrue(any("Ref_Leaf_2_002" in link.findtext("Name")
                                or "Ref_Leaf_2_003" in link.findtext("Name")
                                for link in document.links))
            self.assertEqual(len(renames), 2)
            self.assertEqual(sync.standardize_base_ref_names(document), [])

    def test_sync_reports_and_removes_follower_only_managed_structure(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.spm"
            target = Path(temp) / "target.spm"
            write_spm(master, make_master())
            target_text = make_target().replace(
                property_xml("Generation:First", "0.95"),
                property_xml("Generation:First", "0.95")
                + property_xml("Generation:Pass", "4"),
                1,
            )
            target_root = ET.fromstring(target_text)
            for link in target_root.find("Links") or []:
                if link.findtext("TargetGUID") == "target-ref":
                    link.find("SourceGUID").text = "target-leaf-base"
            target_text = ET.tostring(target_root, encoding="unicode")
            write_spm(target, target_text)
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
                "End 2": "End",
            }
            delta = sync.compare_base_structure(
                sync.SPMDocument.from_path(master, full=True),
                sync.SPMDocument.from_path(target, full=True),
                mapping,
                include_details=True,
            )
            self.assertEqual(delta["master_sync"], 0)
            self.assertEqual(delta["master_sync_details"], [])
            self.assertEqual(delta["target_local"], 0)
            self.assertEqual(delta["target_local_details"], [])
            self.assertGreaterEqual(delta["remove"], 1)
            self.assertTrue(any(
                detail["name"] == "Knot unique"
                for detail in delta["remove_details"]
            ))

            plan = sync.build_sync_plan(master, target, mapping)
            self.assertTrue(plan.changed)
            self.assertGreaterEqual(plan.added_nodes, 1)
            self.assertEqual(plan.master_sync_nodes, 0)
            self.assertFalse(plan.mapping_required)
            self.assertEqual(
                sum(result.removed_nodes for result in plan.base_results),
                1,
            )
            self.assertTrue(any(
                detail["name"] == "Knot unique"
                for result in plan.base_results
                for detail in result.removed_node_details
            ))
            self.assertTrue(any(
                detail["name"] == "Leaf 2"
                for result in plan.base_results
                for detail in result.added_node_details
            ))
            self.assertEqual(
                plan.renamed_bases,
                {},
            )
            self.assertEqual(plan.base_ref_filter_updates, 0)

            patched = sync.SPMDocument(target, plan.patched_text, True, full=True)
            patched.validate(plan.patched_text)
            branch = patched.by_guid["target-leaf-branch"]
            self.assertEqual(property_value(branch, "Generation:First"), "0.25")
            self.assertEqual(property_value(branch, "Generation:Pass"), "4")
            self.assertEqual(property_value(branch, "Random Seeds:Generation"), "888")
            self.assertEqual(property_value(branch, "Materials:Branch:0:Material"), "TargetBark")
            self.assertNotIn("target-knot", patched.by_guid)
            self.assertFalse(any(
                link.findtext("SourceGUID") == "target-knot"
                or link.findtext("TargetGUID") == "target-knot"
                for link in patched.links
            ))
            self.assertTrue(all(
                link.findtext("SourceGUID") in patched.by_guid
                and link.findtext("TargetGUID") in patched.by_guid
                for link in patched.links
            ))

            leaf_base = patched.by_guid["target-leaf-base"]
            leaf_ref = patched.by_guid["target-ref"]
            independent_base = patched.by_guid["target-branch-small"]
            independent_child = patched.by_guid["target-branch-child-2"]
            self.assertEqual(patched.generator_name(leaf_base), "Leaf 2")
            self.assertEqual(patched.base_ref_filter(leaf_ref), "Leaf 2")
            self.assertEqual(patched.parent["target-ref"], "target-leaf-base")
            self.assertEqual(
                patched.generator_name(independent_base),
                "BranchSmall",
            )
            self.assertEqual(
                patched.generator_name(independent_child),
                "Branch 81",
            )
            for generator in (leaf_base, branch, leaf_ref):
                self.assertEqual(generator.findtext("Extra/m_bSetBackgroundIconColor"), "true")
                self.assertEqual(generator.findtext("Extra/m_vecBackgroundIconColor_g"), "1")
            # Existing target identity is unchanged and one missing Leaf Mesh was added.
            self.assertIn("target-leaf-base", patched.by_guid)
            leaf_meshes = [
                item for item in patched.generators
                if item.attrib.get("Type") == "Leaf Mesh"
            ]
            self.assertEqual(len(leaf_meshes), 2)

    def test_group_sync_removes_follower_extra_without_master_or_sibling_effects(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            first = folder / "tree_02.spm"
            second = folder / "tree_03.spm"
            write_spm(master, make_master())
            write_spm(first, make_target())
            write_spm(second, make_target_without_knot())
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
                "End 2": "End",
            }
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {
                "Leaf": "leaf",
                "Branch": "branch",
                "End": "end",
            }
            sync.assign_follower(
                manifest, master.name, first.name, mapping, confirmed=True
            )
            sync.assign_follower(
                manifest, master.name, second.name, mapping, confirmed=True
            )
            sync.save_manifest(folder, manifest)
            original_master_bytes = master.read_bytes()

            original_from_path = sync.SPMDocument.from_path
            with mock.patch.object(
                sync.SPMDocument,
                "from_path",
                side_effect=original_from_path,
            ) as from_path:
                result = sync.apply_group_transaction(
                    folder,
                    master.name,
                    [first.name, second.name],
                    verify_speedtree=False,
                    verify_callback=lambda _path: None,
                )
            full_reads = [
                Path(call.args[0]).name
                for call in from_path.call_args_list
                if call.kwargs.get("full", True)
            ]
            self.assertEqual(full_reads.count(first.name), 1)
            self.assertEqual(full_reads.count(second.name), 1)

            self.assertEqual(result["status"], "applied")
            self.assertEqual(
                result["plans"][0].master_sync_nodes,
                0,
            )
            self.assertEqual(
                result["plans"][1].master_sync_nodes,
                0,
            )
            self.assertEqual(
                sum(
                    item.removed_nodes
                    for item in result["plans"][0].base_results
                ),
                1,
            )
            self.assertEqual(
                sum(
                    item.removed_nodes
                    for item in result["plans"][1].base_results
                ),
                0,
            )
            self.assertEqual(master.read_bytes(), original_master_bytes)
            self.assertNotIn(str(master), result["changed_files"])
            master_doc = sync.SPMDocument.from_path(master, full=True)
            first_doc = sync.SPMDocument.from_path(first, full=True)
            second_doc = sync.SPMDocument.from_path(second, full=True)
            self.assertEqual(
                len([
                    item for item in master_doc.generators
                    if master_doc.generator_name(item) == "Knot unique"
                ]),
                0,
            )
            self.assertNotIn("target-knot", first_doc.by_guid)
            self.assertFalse(any(
                link.findtext("SourceGUID") == "target-knot"
                or link.findtext("TargetGUID") == "target-knot"
                for link in first_doc.links
            ))
            self.assertTrue(all(
                link.findtext("SourceGUID") in first_doc.by_guid
                and link.findtext("TargetGUID") in first_doc.by_guid
                for link in first_doc.links
            ))
            self.assertEqual(
                len([
                    item for item in second_doc.generators
                    if second_doc.generator_name(item) == "Knot unique"
                ]),
                0,
            )
            first_delta = sync.compare_base_structure(
                master_doc, first_doc, mapping, include_details=True
            )
            second_delta = sync.compare_base_structure(
                master_doc, second_doc, mapping, include_details=True
            )
            self.assertEqual(first_delta["missing"], 0)
            self.assertEqual(first_delta["master_sync"], 0)
            self.assertEqual(first_delta["remove"], 0)
            self.assertEqual(second_delta["missing"], 0)
            self.assertEqual(second_delta["master_sync"], 0)
            self.assertEqual(second_delta["remove"], 0)

            again = sync.apply_group_transaction(
                folder,
                master.name,
                [first.name, second.name],
                verify_speedtree=False,
                verify_callback=lambda _path: None,
            )
            self.assertEqual(again["status"], "up_to_date")

    def test_same_role_bases_receive_distinct_stable_brightness(self):
        big = sync.base_role_color("branch", "BranchBig")
        small = sync.base_role_color("branch", "BranchSmall")
        self.assertEqual(big[2], 1.0)
        self.assertLess(small[2], big[2])
        self.assertEqual(small, sync.base_role_color("branch", "BranchSmall"))

    def test_sync_preserves_receipt_owned_legacy_foreground(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.spm"
            target = Path(temp) / "target.spm"
            write_spm(master, make_master())
            target_root = ET.fromstring(make_target())
            legacy = next(
                item for item in target_root.findall("./Generators/Generator")
                if item.findtext("GUID") == "target-leaf-mesh"
            )
            extra = legacy.find("Extra")
            for tag, value in LEGACY_CLUSTER_MARKER_VALUES.items():
                child = extra.find(tag)
                if child is None:
                    child = ET.SubElement(extra, tag)
                child.text = value
            write_spm(target, ET.tostring(target_root, encoding="unicode"))
            receipt = marker_receipt_path(target)
            receipt.parent.mkdir()
            receipt.write_text(json.dumps({
                "kind": RECEIPT_KIND,
                "version": RECEIPT_VERSION,
                "status": "applied",
                "spm": str(target),
                "generator_guids": ["target-leaf-mesh"],
                "entries": {},
            }), encoding="utf-8")
            self.assertEqual(
                sync.inspect_legacy_cluster_state(target)[
                    "classified_generator_guids"
                ],
                ["target-leaf-mesh"],
            )

            plan = sync.build_sync_plan(
                master,
                target,
                {
                    "Leaf 2": "Leaf",
                    "BranchBig": "Branch",
                    "BranchSmall": None,
                    "End 2": "End",
                },
            )
            patched = sync.SPMDocument(
                target, plan.patched_text, True, full=True
            )
            generator = patched.by_guid["target-leaf-mesh"]

            self.assertEqual(
                {
                    tag: generator.findtext(f"Extra/{tag}")
                    for tag in LEGACY_CLUSTER_MARKER_VALUES
                },
                LEGACY_CLUSTER_MARKER_VALUES,
            )
            self.assertEqual(
                generator.findtext("Extra/m_bSetBackgroundIconColor"),
                "true",
            )

    def test_missing_master_base_is_added_under_tree_and_mapping_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_03.spm"
            write_spm(master, make_master())
            write_spm(target, make_target_without_end())
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
            }

            plan = sync.build_sync_plan(master, target, mapping)
            self.assertFalse(plan.mapping_required)
            self.assertEqual(plan.added_base_mappings, {"End": "End"})
            end_result = next(item for item in plan.base_results if item.source_base == "End")
            self.assertTrue(end_result.created_base)
            self.assertEqual(end_result.added_nodes, 3)
            patched = sync.SPMDocument(target, plan.patched_text, True, full=True)
            end_base = patched.resolve_base("End")
            self.assertIsNotNone(end_base)
            parent = patched.by_guid[patched.parent[patched.generator_guid(end_base)]]
            self.assertEqual(patched.generator_type(parent), "Tree")

            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {"Leaf": "leaf", "Branch": "branch", "End": "end"}
            sync.assign_follower(manifest, master.name, target.name, mapping, confirmed=True)
            sync.save_manifest(folder, manifest)
            sync.apply_group_transaction(
                folder, master.name, verify_speedtree=False,
                verify_callback=lambda _path: None,
            )

            current = sync.load_manifest(folder)
            follower = sync.find_group(current, master.name)["followers"][0]
            self.assertEqual(follower["base_map"]["End"], "End")
            written = sync.SPMDocument.from_path(target, full=True)
            self.assertEqual(
                len([base for base in written.base_nodes() if written.generator_name(base) == "End"]),
                1,
            )
            next_plan = sync.build_sync_plan(master, target, follower["base_map"])
            self.assertFalse(next_plan.added_base_mappings)
            self.assertFalse(next_plan.mapping_required)

    def test_tree_only_target_without_links_accepts_all_master_bases(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "tree_01.spm"
            target = Path(temp) / "tree_04.spm"
            write_spm(master, make_master())
            write_spm(target, make_empty_target_without_links())

            prefix = sync.SPMDocument.from_path(target, full=False)
            self.assertEqual(len(prefix.generators), 1)
            self.assertFalse(prefix.links)
            plan = sync.build_sync_plan(master, target, {})
            self.assertFalse(plan.mapping_required)
            self.assertEqual(
                plan.added_base_mappings,
                {"Leaf": "Leaf", "Branch": "Branch", "End": "End"},
            )
            self.assertEqual(
                {item.source_base for item in plan.base_results if item.created_base},
                {"Leaf", "Branch", "End"},
            )
            self.assertIn("<Links", plan.patched_text)
            patched = sync.SPMDocument(target, plan.patched_text, True, full=True)
            patched.validate(plan.patched_text)
            self.assertEqual(len(patched.base_nodes()), 3)

    def test_nested_self_closing_generators_do_not_shadow_top_level_section(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "forces_before_generators.spm"
            text = make_master().replace(
                "<Generators>",
                "<Forces><Force><Extra><Generators /></Extra></Force></Forces><Generators>",
                1,
            )
            write_spm(path, text)
            document = sync.SPMDocument.from_path(path, full=True)
            self.assertEqual(
                {document.generator_name(base) for base in document.base_nodes()},
                {"Leaf", "Branch", "End"},
            )

    def test_integrity_detects_broken_links(self):
        broken = make_master().replace("<TargetGUID>leaf-branch</TargetGUID>",
                                       "<TargetGUID>missing</TargetGUID>", 1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.spm"
            write_spm(path, broken)
            document = sync.SPMDocument.from_path(path, full=True)
            errors = document.integrity_errors()
            self.assertTrue(any("Link target GUID 없음" in item for item in errors))

    def test_multi_property_containers_stay_with_the_follower(self):
        # The indexed slot properties are protected, so the container that
        # counts them must be too - otherwise the follower claims the master's
        # slot count and SpeedTree draws "Material: None / Mesh: Any" slots.
        for container in ("Leaves:Type", "Material:Frond", "Cap:Material"):
            self.assertTrue(sync.is_protected_property(container), container)
            self.assertTrue(sync.is_protected_property(f"{container}:1:Material"))
            self.assertTrue(sync.is_protected_property(f"{container}:1:Mesh"))

    def test_sync_keeps_follower_leaf_slot_count(self):
        def leaf_generator(child_count, slots):
            generator = ET.Element("Generator", {"Type": "Leaf Mesh"})
            properties = ET.SubElement(generator, "Properties")
            parent = ET.SubElement(properties, "Property")
            ET.SubElement(parent, "Name").text = "Leaves:Type"
            ET.SubElement(parent, "MultiPropertyChildren").text = str(child_count)
            ET.SubElement(parent, "Value").text = "0"
            for index, material in enumerate(slots):
                for suffix, value in (("Material", material), ("Mesh", "-10")):
                    prop = ET.SubElement(properties, "Property")
                    ET.SubElement(prop, "Name").text = f"Leaves:Type:{index}:{suffix}"
                    ET.SubElement(prop, "Value").text = value
            unprotected = ET.SubElement(properties, "Property")
            ET.SubElement(unprotected, "Name").text = "Generation:Style"
            ET.SubElement(unprotected, "Value").text = str(child_count)
            return generator

        master = leaf_generator(3, ["7", "7", "7"])
        follower = leaf_generator(1, ["8"])
        changes, names = sync.sync_generator_properties(master, follower)

        parent = next(
            item for item in follower.find("Properties")
            if item.findtext("Name") == "Leaves:Type"
        )
        self.assertEqual(parent.findtext("MultiPropertyChildren"), "1")
        self.assertNotIn("Leaves:Type", names)
        self.assertIn("Generation:Style", names)
        self.assertEqual(changes, 1)

    def test_speedtree_base_filter_search_syntax(self):
        self.assertTrue(sync.is_protected_property(sync.GENERATION_PASS_PROPERTY))
        self.assertFalse(sync.is_protected_property("Generation:Shared:Pass override"))
        self.assertTrue(sync.speedtree_search_matches("Leaf 2", "Leaf"))
        self.assertFalse(sync.speedtree_search_matches("Leaf 2", "=Leaf"))
        self.assertTrue(sync.speedtree_search_matches("Leaf", "=leaf"))
        self.assertFalse(sync.speedtree_search_matches("leaf", "==Leaf"))
        self.assertTrue(sync.speedtree_search_matches("BranchBig", "Leaf | Branch*"))
        self.assertFalse(sync.speedtree_search_matches(
            "BranchSmall", "(Leaf | Branch*) & !Small"
        ))
        self.assertTrue(sync.speedtree_search_matches("Leaf|Branch", '"Leaf|Branch"'))
        with self.assertRaises(sync.SearchSyntaxError):
            sync.speedtree_search_matches("Leaf", "Leaf |")

    def test_integrity_detects_and_repairs_reference_pass_regression(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "target.spm"
            write_spm(path, make_pass_target(base_pass=2, ref_pass=2, parent_pass=3))
            document = sync.SPMDocument.from_path(path, full=True)

            errors = document.integrity_errors()
            self.assertTrue(any("Generation Pass 조상 순서 오류" in item for item in errors))
            self.assertTrue(any("Reference/Base Pass 순서 오류" in item for item in errors))

            adjustments = sync.repair_generation_passes(document)
            by_name = {item["name"]: item for item in adjustments}
            self.assertEqual(by_name["Leaf reference"]["new_pass"], 3)
            self.assertEqual(by_name["Leaf"]["new_pass"], 4)
            self.assertEqual(document.generator_pass(document.by_guid["target-ref"]), 3)
            self.assertEqual(document.generator_pass(document.by_guid["target-leaf-base"]), 4)
            # Do not propagate the Base scheduling pass into its template subtree.
            self.assertEqual(document.generator_pass(document.by_guid["target-leaf-child"]), 1)
            document.validate(document.render())

    def test_sync_protects_target_pass_and_reports_minimum_safe_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.spm"
            target = Path(temp) / "target.spm"
            write_spm(master, make_pass_master(base_pass=2))
            write_spm(target, make_pass_target(base_pass=2, ref_pass=2, parent_pass=3))

            plan = sync.build_sync_plan(master, target, {"Leaf": "Leaf"})
            patched = sync.SPMDocument(target, plan.patched_text, True, full=True)
            self.assertEqual(patched.generator_pass(patched.by_guid["target-ref"]), 3)
            self.assertEqual(patched.generator_pass(patched.by_guid["target-leaf-base"]), 4)
            self.assertEqual(len(plan.pass_adjustments), 2)
            self.assertNotIn(
                sync.GENERATION_PASS_PROPERTY,
                [
                    name
                    for result in plan.base_results
                    for name in result.changed_properties
                ],
            )

            write_spm(target, plan.patched_text)
            repeat = sync.build_sync_plan(master, target, {"Leaf": "Leaf"})
            self.assertFalse(repeat.pass_adjustments)

    def test_valid_target_pass_stays_higher_than_master_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.spm"
            target = Path(temp) / "target.spm"
            write_spm(master, make_pass_master(base_pass=2))
            write_spm(target, make_pass_target(base_pass=4, ref_pass=2, parent_pass=2))

            plan = sync.build_sync_plan(master, target, {"Leaf": "Leaf"})
            patched = sync.SPMDocument(target, plan.patched_text, True, full=True)
            self.assertEqual(patched.generator_pass(patched.by_guid["target-ref"]), 2)
            self.assertEqual(patched.generator_pass(patched.by_guid["target-leaf-base"]), 4)
            self.assertFalse(plan.pass_adjustments)

    def test_blank_base_filter_repairs_every_available_base(self):
        root = ET.fromstring(
            make_pass_target(base_pass=1, ref_pass=1, parent_pass=1, base_filter="")
        )
        generators = root.find("Generators")
        links = root.find("Links")
        second_base = copy.deepcopy(next(
            item for item in generators if item.findtext("GUID") == "target-leaf-base"
        ))
        second_base.find("GUID").text = "target-branch-base"
        second_base.find("Name").text = "Branch"
        generators.append(second_base)
        links.append(ET.fromstring(link_xml(
            "target-tree-branch", "target-tree", "target-branch-base", "Tree->Branch"
        )))

        document = sync.SPMDocument(
            Path("blank-filter.spm"), ET.tostring(root, encoding="unicode"), True, full=True
        )
        adjustments = sync.repair_generation_passes(document)
        self.assertEqual(
            {item["name"] for item in adjustments},
            {"Leaf", "Branch"},
        )
        self.assertTrue(all(
            document.generator_pass(base) == 2 for base in document.base_nodes()
        ))
        document.validate(document.render())

    def test_invalid_generation_pass_is_a_hard_integrity_error(self):
        root = ET.fromstring(make_pass_target())
        ref = next(
            item for item in root.find("Generators")
            if item.findtext("GUID") == "target-ref"
        )
        next(
            prop for prop in ref.findall("./Properties/Property")
            if prop.findtext("Name") == sync.GENERATION_PASS_PROPERTY
        ).find("Value").text = "not-an-integer"
        document = sync.SPMDocument(
            Path("bad-pass.spm"), ET.tostring(root, encoding="unicode"), True, full=True
        )
        with self.assertRaisesRegex(sync.SyncError, "정수가 아닙니다"):
            document.validate()

    def test_pass_only_repair_transaction_backs_up_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "target.spm"
            autosave = Path(temp) / "~target.sbk"
            write_spm(path, make_pass_target(base_pass=2, ref_pass=2, parent_pass=3))
            autosave.write_bytes(b"preserve-live-autosave")
            original = path.read_bytes()
            verified = []

            result = sync.apply_pass_repair_transaction(
                path, verify_callback=lambda candidate: verified.append(Path(candidate).name)
            )
            self.assertEqual(result["status"], "applied")
            self.assertEqual(len(result["pass_adjustments"]), 2)
            self.assertEqual(len(verified), 1)
            self.assertTrue(verified[0].startswith("__spm_sync_preflight_"))
            backup_dir = Path(result["backup_dir"])
            self.assertEqual((backup_dir / "01_target.spm").read_bytes(), original)
            self.assertEqual(
                (backup_dir / "02_~target.sbk").read_bytes(),
                b"preserve-live-autosave",
            )
            written = sync.SPMDocument.from_path(path, full=True)
            self.assertEqual(written.generator_pass(written.by_guid["target-ref"]), 3)
            self.assertEqual(written.generator_pass(written.by_guid["target-leaf-base"]), 4)

            repeat = sync.apply_pass_repair_transaction(path)
            self.assertEqual(repeat["status"], "unchanged")
            self.assertIsNone(repeat["backup_dir"])

    def test_pass_only_repair_preflight_failure_never_writes_original(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "target.spm"
            write_spm(path, make_pass_target(base_pass=2, ref_pass=2, parent_pass=3))
            original = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "preflight failed"):
                sync.apply_pass_repair_transaction(
                    path,
                    verify_callback=lambda _candidate: (_ for _ in ()).throw(
                        RuntimeError("preflight failed")
                    ),
                )
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((Path(temp) / sync.BACKUP_SUBDIR).exists())

    def test_pass_only_repair_aborts_if_file_changes_during_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "target.spm"
            write_spm(path, make_pass_target(base_pass=2, ref_pass=2, parent_pass=3))
            external_text = make_pass_target(base_pass=5, ref_pass=2, parent_pass=2)
            external_bytes = gzip.compress(external_text.encode("utf-8"), mtime=0)

            def mutate_original(_candidate):
                path.write_bytes(external_bytes)

            with self.assertRaisesRegex(sync.SyncError, "사전검사 중 파일이 변경"):
                sync.apply_pass_repair_transaction(path, verify_callback=mutate_original)
            self.assertEqual(path.read_bytes(), external_bytes)
            self.assertFalse((Path(temp) / sync.BACKUP_SUBDIR).exists())

    def test_transaction_rolls_back_every_file_on_verification_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {"Leaf": "leaf", "Branch": "branch", "End": "end"}
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
                "End 2": "End",
            }
            sync.assign_follower(manifest, master.name, target.name, mapping, confirmed=True)
            sync.save_manifest(folder, manifest)
            originals = {master: master.read_bytes(), target: target.read_bytes()}

            def fail(_path):
                raise RuntimeError("verification failed")

            with self.assertRaises(RuntimeError):
                sync.apply_group_transaction(
                    folder, master.name, verify_speedtree=False, verify_callback=fail
                )
            self.assertEqual(master.read_bytes(), originals[master])
            self.assertEqual(target.read_bytes(), originals[target])
            current = sync.load_manifest(folder)
            follower = sync.find_group(current, master.name)["followers"][0]
            self.assertIsNone(follower["last_sync"])
            backups = list((folder / sync.BACKUP_SUBDIR).glob("generator_sync_*"))
            self.assertEqual(len(backups), 1)

    def test_transaction_aborts_when_another_tool_rewrites_an_spm(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {"Leaf": "leaf", "Branch": "branch", "End": "end"}
            sync.assign_follower(
                manifest, master.name, target.name,
                {"Leaf 2": "Leaf", "BranchBig": "Branch",
                 "BranchSmall": None, "End 2": "End"},
                confirmed=True,
            )
            sync.save_manifest(folder, manifest)

            concurrent = make_target().replace("<Name>Leaf 2</Name>", "<Name>Leaf 9</Name>", 1)

            def preflight(*_args, **_kwargs):
                # Stand in for a Cluster relationship ON finishing while the
                # SpeedTree preflight is still running.
                write_spm(target, concurrent)

            with mock.patch.object(sync, "verify_temporary_patches", side_effect=preflight):
                with self.assertRaises(sync.SyncError) as caught:
                    sync.apply_group_transaction(
                        folder, master.name, verify_speedtree=True,
                        speedtree_exe=Path("speedtree.exe"),
                        xml_ini=Path("options.ini"),
                    )
            self.assertIn(target.name, str(caught.exception))
            self.assertEqual(
                sync.read_spm_text(target)[0], concurrent,
                "the concurrent write must survive",
            )
            self.assertFalse((folder / sync.BACKUP_SUBDIR).exists())

    def test_transaction_aborts_when_target_changes_during_plan_read(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {
                "Leaf": "leaf",
                "Branch": "branch",
                "End": "end",
            }
            sync.assign_follower(
                manifest,
                master.name,
                target.name,
                {
                    "Leaf 2": "Leaf",
                    "BranchBig": "Branch",
                    "BranchSmall": None,
                    "End 2": "End",
                },
                confirmed=True,
            )
            sync.save_manifest(folder, manifest)
            concurrent = make_target().replace(
                "<Name>Leaf 2</Name>",
                "<Name>Leaf 9</Name>",
                1,
            )
            original_load = sync.SPMDocument.from_path
            changed = False

            def load_then_change(path, full=True):
                nonlocal changed
                document = original_load(path, full=full)
                if Path(path) == target and not changed:
                    changed = True
                    write_spm(target, concurrent)
                return document

            with mock.patch.object(
                sync.SPMDocument,
                "from_path",
                side_effect=load_then_change,
            ):
                with self.assertRaises(sync.SyncError):
                    sync.apply_group_transaction(
                        folder,
                        master.name,
                        verify_speedtree=False,
                    )

            self.assertEqual(sync.read_spm_text(target)[0], concurrent)
            self.assertFalse((folder / sync.BACKUP_SUBDIR).exists())

    def test_successful_transaction_updates_manifest_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {"Leaf": "leaf", "Branch": "branch", "End": "end"}
            mapping = {
                "Leaf 2": "Leaf", "BranchBig": "Branch",
                "BranchSmall": None, "End 2": "End",
            }
            sync.assign_follower(manifest, master.name, target.name, mapping, confirmed=True)
            sync.save_manifest(folder, manifest)
            original_master_bytes = master.read_bytes()
            verified = []
            progress = []
            result = sync.apply_group_transaction(
                folder, master.name, verify_speedtree=False,
                verify_callback=lambda path: verified.append(Path(path).name),
                progress_callback=lambda stage, percent: progress.append((stage, percent)),
            )
            self.assertEqual(result["status"], "applied")
            backup_dir = Path(result["backup_dir"])
            self.assertTrue(backup_dir.is_dir())
            backup_names = {path.name for path in backup_dir.iterdir()}
            self.assertIn(f"00_{sync.MANIFEST_NAME}", backup_names)
            self.assertFalse(any(name.endswith(master.name) for name in backup_names))
            self.assertTrue(any(name.endswith(target.name) for name in backup_names))
            self.assertEqual(verified, [target.name])
            self.assertEqual(result["changed_files"], [str(target)])
            self.assertEqual(master.read_bytes(), original_master_bytes)
            self.assertEqual(progress[-1][1], 100)
            self.assertEqual(
                [percent for _stage, percent in progress],
                sorted(percent for _stage, percent in progress),
            )
            progress_text = "\n".join(stage for stage, _percent in progress)
            self.assertIn("패치 계산", progress_text)
            self.assertIn("백업", progress_text)
            self.assertIn("저장", progress_text)
            current = sync.load_manifest(folder)
            follower = sync.find_group(current, master.name)["followers"][0]
            self.assertTrue(follower["last_sync"])
            self.assertTrue(follower["last_master_hash"])
            self.assertEqual(follower["base_map"]["Leaf 2"], "Leaf")
            self.assertEqual(follower["base_map"]["BranchBig"], "Branch")
            self.assertEqual(follower["base_map"]["End 2"], "End")

    def test_up_to_date_transaction_rechecks_sources_before_manifest_save(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {
                "Leaf": "leaf",
                "Branch": "branch",
                "End": "end",
            }
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
                "End 2": "End",
            }
            sync.assign_follower(
                manifest,
                master.name,
                target.name,
                mapping,
                confirmed=True,
            )
            sync.save_manifest(folder, manifest)
            sync.apply_group_transaction(
                folder,
                master.name,
                verify_speedtree=False,
            )
            manifest_before = (folder / sync.MANIFEST_NAME).read_bytes()
            original_assert = sync._assert_spm_unchanged

            def rewrite_before_check(fingerprints):
                target.write_bytes(target.read_bytes() + b"\n")
                original_assert(fingerprints)

            with mock.patch.object(
                sync,
                "_assert_spm_unchanged",
                side_effect=rewrite_before_check,
            ):
                with self.assertRaises(sync.SyncError):
                    sync.apply_group_transaction(
                        folder,
                        master.name,
                        verify_speedtree=False,
                    )

            self.assertEqual(
                (folder / sync.MANIFEST_NAME).read_bytes(),
                manifest_before,
            )

    def test_manifest_save_failure_rolls_back_all_spms(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {"Leaf": "leaf", "Branch": "branch", "End": "end"}
            mapping = {
                "Leaf 2": "Leaf", "BranchBig": "Branch",
                "BranchSmall": None, "End 2": "End",
            }
            sync.assign_follower(manifest, master.name, target.name, mapping, confirmed=True)
            sync.save_manifest(folder, manifest)
            originals = {master: master.read_bytes(), target: target.read_bytes()}

            with mock.patch.object(sync, "save_manifest", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    sync.apply_group_transaction(
                        folder, master.name, verify_speedtree=False,
                        verify_callback=lambda _path: None,
                    )

            self.assertEqual(master.read_bytes(), originals[master])
            self.assertEqual(target.read_bytes(), originals[target])

    def test_cancellation_during_write_rolls_back_the_whole_group(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            first = folder / "tree_02.spm"
            second = folder / "tree_03.spm"
            write_spm(master, make_master())
            write_spm(first, make_target())
            write_spm(second, make_target())
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {
                "Leaf": "leaf",
                "Branch": "branch",
                "End": "end",
            }
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
                "End 2": "End",
            }
            sync.assign_follower(
                manifest, master.name, first.name, mapping, confirmed=True
            )
            sync.assign_follower(
                manifest, master.name, second.name, mapping, confirmed=True
            )
            sync.save_manifest(folder, manifest)
            originals = {
                first: first.read_bytes(),
                second: second.read_bytes(),
                folder / sync.MANIFEST_NAME: (
                    folder / sync.MANIFEST_NAME
                ).read_bytes(),
            }
            cancel = threading.Event()

            def progress(stage, _percent):
                if "검증된 SPM 저장 중" in stage and "(2/2)" in stage:
                    cancel.set()

            with self.assertRaises(sync.SyncCancelled) as raised:
                sync.apply_group_transaction(
                    folder,
                    master.name,
                    [first.name, second.name],
                    verify_speedtree=False,
                    verify_callback=lambda _path: None,
                    progress_callback=progress,
                    cancel_requested=cancel.is_set,
                )

            self.assertEqual(
                raised.exception.termination_state,
                "cancelled_at_safe_boundary",
            )
            for path, expected in originals.items():
                self.assertEqual(path.read_bytes(), expected)

    def test_rollback_failure_preserves_original_error_and_stable_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            target_before = target.read_bytes()
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {
                "Leaf": "leaf",
                "Branch": "branch",
                "End": "end",
            }
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
                "End 2": "End",
            }
            sync.assign_follower(
                manifest,
                master.name,
                target.name,
                mapping,
                confirmed=True,
            )
            sync.save_manifest(folder, manifest)
            manifest_before = (folder / sync.MANIFEST_NAME).read_bytes()
            real_copy2 = sync.shutil.copy2

            def fail_target_restore(source, destination, *args, **kwargs):
                source = Path(source)
                destination = Path(destination)
                if (
                    source.parent.name.startswith("generator_sync_")
                    and destination == target
                ):
                    raise OSError("restore access denied")
                return real_copy2(source, destination, *args, **kwargs)

            with mock.patch.object(
                sync.shutil,
                "copy2",
                side_effect=fail_target_restore,
            ):
                with self.assertRaises(sync.TransactionRollbackError) as raised:
                    sync.apply_group_transaction(
                        folder,
                        master.name,
                        verify_speedtree=False,
                        verify_callback=lambda _path: (_ for _ in ()).throw(
                            RuntimeError("post_write_validation_failed")
                        ),
                    )

            error = raised.exception
            self.assertEqual(error.reason_token, "transaction_rollback_failed")
            self.assertIn("post_write_validation_failed", error.original_error)
            self.assertTrue(error.rollback_errors)
            self.assertIn("restore access denied", str(error.rollback_errors))
            self.assertIn("transaction_rollback_failed", str(error))
            self.assertNotEqual(target.read_bytes(), target_before)
            self.assertEqual(
                (folder / sync.MANIFEST_NAME).read_bytes(),
                manifest_before,
            )

    def test_transaction_preserves_concurrent_manifest_edit_and_rolls_back_spm(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            master = folder / "tree_01.spm"
            target = folder / "tree_02.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            target_before = target.read_bytes()
            manifest = sync.default_manifest()
            sync.set_master(manifest, master.name)
            group = sync.find_group(manifest, master.name)
            group["base_categories"] = {
                "Leaf": "leaf",
                "Branch": "branch",
                "End": "end",
            }
            sync.assign_follower(
                manifest,
                master.name,
                target.name,
                {
                    "Leaf 2": "Leaf",
                    "BranchBig": "Branch",
                    "BranchSmall": None,
                    "End 2": "End",
                },
                confirmed=True,
            )
            sync.save_manifest(folder, manifest)
            concurrent = copy.deepcopy(manifest)
            concurrent["independent"] = ["external_edit.spm"]

            def edit_manifest_after_spm_write(_path):
                sync.save_manifest(folder, concurrent)

            with self.assertRaises(sync.SyncError):
                sync.apply_group_transaction(
                    folder,
                    master.name,
                    verify_speedtree=False,
                    verify_callback=edit_manifest_after_spm_write,
                )

            self.assertEqual(target.read_bytes(), target_before)
            self.assertEqual(
                sync.load_manifest(folder)["independent"],
                ["external_edit.spm"],
            )


if __name__ == "__main__":
    unittest.main()
