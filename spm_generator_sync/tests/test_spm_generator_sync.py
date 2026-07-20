import copy
import gzip
import json
import re
import sys
import tempfile
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

    def test_sync_preserves_identity_assets_and_target_only_nodes(self):
        with tempfile.TemporaryDirectory() as temp:
            master = Path(temp) / "master.spm"
            target = Path(temp) / "target.spm"
            write_spm(master, make_master())
            write_spm(target, make_target())
            mapping = {
                "Leaf 2": "Leaf",
                "BranchBig": "Branch",
                "BranchSmall": None,
                "End 2": "End",
            }
            plan = sync.build_sync_plan(master, target, mapping)
            self.assertTrue(plan.changed)
            self.assertGreaterEqual(plan.added_nodes, 1)
            self.assertGreaterEqual(plan.target_only_nodes, 1)
            self.assertFalse(plan.mapping_required)
            self.assertTrue(any(
                detail["name"] == "Leaf 2"
                for result in plan.base_results
                for detail in result.added_node_details
            ))
            self.assertTrue(any(
                detail["name"] == "Knot unique"
                for result in plan.base_results
                for detail in result.target_only_details
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
            self.assertEqual(property_value(branch, "Random Seeds:Generation"), "888")
            self.assertEqual(property_value(branch, "Materials:Branch:0:Material"), "TargetBark")
            self.assertIn("target-knot", patched.by_guid)

            leaf_base = patched.by_guid["target-leaf-base"]
            leaf_ref = patched.by_guid["target-ref"]
            self.assertEqual(patched.generator_name(leaf_base), "Leaf 2")
            self.assertEqual(patched.base_ref_filter(leaf_ref), "Leaf 2")
            for generator in (leaf_base, branch, leaf_ref):
                self.assertEqual(generator.findtext("Extra/m_bSetBackgroundIconColor"), "true")
                self.assertEqual(generator.findtext("Extra/m_vecBackgroundIconColor_g"), "1")
            unique_knot = patched.by_guid["target-knot"]
            self.assertEqual(unique_knot.findtext("Extra/m_bSetForegroundIconColor"), "true")
            self.assertEqual(unique_knot.findtext("Extra/m_vecForegroundIconColor_r"), "1")
            self.assertLess(
                float(unique_knot.findtext("Extra/m_vecBackgroundIconColor_g")), 0.5
            )
            # Existing target identity is unchanged and one missing Leaf Mesh was added.
            self.assertIn("target-leaf-base", patched.by_guid)
            leaf_meshes = [
                item for item in patched.generators
                if item.attrib.get("Type") == "Leaf Mesh"
            ]
            self.assertEqual(len(leaf_meshes), 2)

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
            self.assertIn(f"01_{master.name}", backup_names)
            self.assertIn(f"02_{target.name}", backup_names)
            self.assertIn(master.name, verified)
            self.assertIn(target.name, verified)
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


if __name__ == "__main__":
    unittest.main()
