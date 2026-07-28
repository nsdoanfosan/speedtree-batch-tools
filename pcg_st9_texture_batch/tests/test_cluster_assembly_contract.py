import gzip
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from pcg_cluster_assembly_contract import (
    ClusterAssemblyReceiptError,
    ClusterAssemblyReceiptStaleError,
    build_cluster_assembly_contract,
    classify_fbx_role,
    cluster_assembly_receipt_resolution,
    file_fingerprint,
    load_cluster_assembly_receipt,
    locate_cluster_assembly_receipt,
    persist_cluster_assembly_receipt,
    persist_cluster_assembly_receipts,
    inspect_fbx_material_mesh_pairs,
    dependency_role,
    _atlas_normalized_variants,
    _canonical_bark_contract,
    _validate_normalized_source_dependency,
)
import pcg_texture_audit as audit_module
from pcg_texture_audit import (
    active_material_ids,
    cluster_material_rename_plan,
    cluster_material_usage,
    cluster_source_inventory,
    cluster_spms,
    current_leaf_atlas_inventory,
    extract_material_image_refs,
    m_prefix_plan,
    prepare_sk,
)
from cluster_spm_pair_contract import inspect_cluster_spm_pair
from atlas_target_registry import save_target_registry


REAL_ELM_SOURCE_FBX = Path(
    r"D:\OneDrive\Forestportfolio\02_nature\Tree\Tree_elm"
    r"\fbx\tree_elm_01.fbx"
)
REAL_ELM_CLUSTER_LEAF = Path(
    r"D:\OneDrive\Forestportfolio\02_nature\Tree\Tree_elm"
    r"\Cluster\leaf_elm_01.spm"
)
REAL_ELM_CLUSTER_BRANCH = Path(
    r"D:\OneDrive\Forestportfolio\02_nature\Tree\Tree_elm"
    r"\Cluster\branch_elm_01.spm"
)


def load_gui_module():
    loader = importlib.machinery.SourceFileLoader(
        "pcg_texture_gui_cluster_contract_test",
        str(TOOL_DIR / "pcg_texture_gui.pyw"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def write_spm(path, materials, mesh_ids=(), active_material_ids=()):
    material_xml = []
    for material_id, name, refs, owned_mesh_ids in materials:
        texture_xml = "".join(
            f"<TexFilename>{value}</TexFilename>" for value in refs)
        cutout_xml = "".join(
            f'<CutoutMesh ID="{value}"/>' for value in owned_mesh_ids)
        material_xml.append(
            f'<Material_v8 ID="{material_id}" Name="{name}">'
            f"{texture_xml}"
            f'<SupplementalCutoutMeshIDs Count="{len(owned_mesh_ids)}">'
            f"{cutout_xml}</SupplementalCutoutMeshIDs>"
            "</Material_v8>"
        )
    mesh_xml = "".join(
        f'<Mesh ID="{value}" Name="mesh-{value}"/>' for value in mesh_ids)
    generator_xml = "".join(
        '<Generator Type="Branch">'
        f'<GUID>generator-{index}</GUID><Hidden>false</Hidden>'
        '<Properties><Property><Name>Geometry:Material</Name>'
        f'<Value>{material_id}</Value></Property></Properties>'
        '</Generator>'
        for index, material_id in enumerate(active_material_ids)
    )
    payload = (
        "<SpeedTree><Materials>" + "".join(material_xml)
        + "</Materials><Meshes>" + mesh_xml
        + "</Meshes><Generators>" + generator_xml
        + "</Generators></SpeedTree>"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as handle:
        handle.write(payload)


def write_ascii_fbx(path, material_names, mesh_names, pairs):
    path.parent.mkdir(parents=True, exist_ok=True)
    object_rows = []
    connection_rows = []
    object_id = 100
    material_ids = {}
    mesh_ids = {}
    for name in material_names:
        material_ids[name] = object_id
        object_rows.append(
            f'    Material: {object_id}, "Material::{name}", "" {{}}')
        object_id += 1
    for name in mesh_names:
        model_id = object_id
        geometry_id = object_id + 1
        mesh_ids[name] = model_id
        object_rows.extend((
            f'    Model: {model_id}, "Model::{name}", "Mesh" {{}}',
            f'    Geometry: {geometry_id}, "Geometry::{name}", "Mesh" {{}}',
        ))
        connection_rows.append(f'    C: "OO",{geometry_id},{model_id}')
        object_id += 2
    for material_name, mesh_name in pairs:
        connection_rows.append(
            f'    C: "OO",{material_ids[material_name]},{mesh_ids[mesh_name]}')
    path.write_text(
        "; FBX 7.4.0 project file\nObjects: {\n"
        + "\n".join(object_rows)
        + "\n}\nConnections: {\n"
        + "\n".join(connection_rows)
        + "\n}\n",
        encoding="utf-8",
    )


class FbxRoleContractTests(unittest.TestCase):
    def test_complete_absent_and_partial_roles_are_independent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fbx = Path(temp_dir) / "tree.fbx"
            write_ascii_fbx(
                fbx,
                material_names=["branch_elm_01_Mat", "leaf_elm_01_Mat"],
                mesh_names=["branch_elm_01", "unrelated_leaf_mesh"],
                pairs=[("branch_elm_01_Mat", "branch_elm_01")],
            )
            report = inspect_fbx_material_mesh_pairs(fbx)

            branch = classify_fbx_role(report, "branch_elm_01")
            leaf = classify_fbx_role(report, "leaf_elm_01")
            fruit = classify_fbx_role(report, "fruit_elm_01")

            self.assertEqual(report["status"], "ok")
            self.assertEqual(branch["status"], "complete_pair")
            self.assertEqual(branch["decision"], "normalize_part")
            self.assertEqual(leaf["status"], "material_without_mesh")
            self.assertEqual(leaf["decision"], "blocked")
            self.assertEqual(fruit["status"], "absent")
            self.assertEqual(fruit["decision"], "pass_through")

    @unittest.skipUnless(REAL_ELM_SOURCE_FBX.is_file(), "Tree Elm source FBX unavailable")
    def test_real_binary_elm_export_contains_both_role_pairs(self):
        report = inspect_fbx_material_mesh_pairs(REAL_ELM_SOURCE_FBX)
        branch = classify_fbx_role(report, "branch_elm_01")
        leaf = classify_fbx_role(report, "leaf_elm_01")

        self.assertEqual(report["format"], "binary")
        self.assertEqual(report["version"], 7700)
        self.assertEqual(branch["decision"], "normalize_part")
        self.assertGreater(branch["complete_pair_count"], 0)
        self.assertEqual(leaf["decision"], "normalize_part")
        self.assertGreater(leaf["complete_pair_count"], 0)


class ClusterAssemblyContractTests(unittest.TestCase):
    def test_no_content_driven_cluster_dependency_does_not_require_bark(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "weed_common_grass"
            target = folder / "SK_weed_common_grass_01.spm"
            write_spm(
                target,
                [("1", "M_leaf_common_grass_01", [], ())],
                active_material_ids=("1",),
            )

            contract = build_cluster_assembly_contract(
                folder,
                [target],
                [],
                cluster_usage={},
            )

            self.assertEqual(
                contract["canonical_bark"]["status"], "not_applicable"
            )
            self.assertEqual(contract["handoff"]["status"], "pass_through")
            self.assertFalse(
                contract["handoff"]["separate_nanite_assembly_requested"]
            )
            self.assertNotIn(
                "CANONICAL_BARK_MISSING",
                [
                    row["code"]
                    for row in contract["handoff"]["errors"]
                ],
            )

    def test_generic_cluster_name_is_a_first_class_assembly_role(self):
        self.assertEqual(
            dependency_role("SK_cluster_densiflora_01"),
            "cluster",
        )

    def test_cluster_source_inventory_uses_canonical_output_names(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            cluster = folder / "Cluster"
            cluster.mkdir()
            generic = cluster / "cluster_Silky_Dogwood_01.spm"
            branch = cluster / "branch_Silky_Dogwood_01.spm"
            generated = cluster / "SK_cluster_Silky_Dogwood_01.spm"
            for path in (generic, branch, generated):
                path.write_bytes(b"spm")

            sources = cluster_spms(folder)
            self.assertEqual(sources, [branch, generated])
            usage = {
                str(generic).lower(): {
                    "spms": [str(folder / "SK_bush_Silky_Dogwood_01.spm")],
                    "material_names": ["cluster_Silky_Dogwood_01"],
                    "source_refs": [str(cluster / "cluster_Silky_Dogwood_01.tga")],
                },
            }
            rows = cluster_source_inventory(sources, usage, {"dependencies": []})

            self.assertEqual([row["name"] for row in rows], [
                "SK_branch_Silky_Dogwood_01", "SK_cluster_Silky_Dogwood_01",
            ])
            generic_row = rows[1]
            self.assertTrue(generic_row["referenced"])
            self.assertEqual(len(generic_row["cluster_output_textures"]), 1)
            self.assertIsNone(generic_row["assembly_role"])
            self.assertEqual(Path(generic_row["authoring_spm"]), generated)
            self.assertEqual(Path(generic_row["output_spm"]), generated)
            self.assertEqual(Path(generic_row["mirror_spm"]), generic)

    def test_missing_connected_cluster_tga_remains_a_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Tree_elm"
            cluster_dir = folder / "Cluster"
            branch = cluster_dir / "branch_elm_01.spm"
            target = folder / "Tree_elm_01.spm"
            write_spm(branch, [("1", "M_Bark_elm_01", [], [])])
            write_spm(target, [(
                "2", "branch_elm_01",
                ["Cluster/branch_elm_01.tga"], ("1",),
            )], mesh_ids=("1",))

            usage = cluster_material_usage([target], [branch])
            branch_usage = usage[str(branch).casefold()]

            expected = str(cluster_dir / "branch_elm_01.tga")
            self.assertEqual(branch_usage["connected_refs"], [expected])
            self.assertEqual(branch_usage["source_refs"], [])
            self.assertEqual(branch_usage["missing_source_refs"], [expected])

            inventory = cluster_source_inventory([branch], usage)
            self.assertEqual(
                inventory[0]["cluster_output_textures"], [expected]
            )
            self.assertEqual(
                inventory[0]["missing_cluster_output_textures"], [expected]
            )

            contract = build_cluster_assembly_contract(
                folder, [target], [branch], cluster_usage=usage
            )
            dependency = contract["dependencies"][0]
            self.assertEqual(
                dependency["tga_basename_validation"]["status"], "missing"
            )
            self.assertEqual(
                dependency["texture_dependencies"][0]["path"], expected
            )
            self.assertIn(
                "CLUSTER_TGA_BASENAME_INVALID",
                [row["code"] for row in contract["handoff"]["errors"]],
            )

            # Missing source data is a handoff error, not a reason for a newly
            # written snapshot receipt to invalidate itself.  Downstream reads
            # the hash-current receipt and reports the real audit error above.
            receipt = persist_cluster_assembly_receipt(
                contract, receipt_dir=Path(temp) / "receipts"
            )
            payload = load_cluster_assembly_receipt(
                receipt, requested_spm=target
            )
            self.assertIn(
                "CLUSTER_TGA_BASENAME_INVALID",
                [
                    row["code"]
                    for row in payload["cluster_assembly"]["handoff"]["errors"]
                ],
            )

    def test_legacy_texture_path_alias_does_not_stale_canonical_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Tree_elm"
            cluster_dir = folder / "Cluster"
            branch = cluster_dir / "branch_elm_01.spm"
            target = folder / "Tree_elm_01.spm"
            canonical = cluster_dir / "branch_elm_01.tga"
            legacy = (
                Path(temp) / "OldOneDrive" / "Tree_elm" / "Cluster"
                / canonical.name
            )
            write_spm(branch, [("1", "M_Bark_elm_01", [], [])])
            write_spm(target, [(
                "2",
                "branch_elm_01",
                [str(legacy), "Cluster/branch_elm_01.tga"],
                ("1",),
            )], mesh_ids=("1",))
            canonical.write_bytes(b"canonical-render")

            usage = cluster_material_usage([target], [branch])
            contract = build_cluster_assembly_contract(
                folder, [target], [branch], cluster_usage=usage
            )
            dependency = contract["dependencies"][0]
            validation = dependency["tga_basename_validation"]

            self.assertEqual(validation["status"], "ok")
            self.assertEqual(validation["refs"], [str(canonical)])
            self.assertEqual(
                validation["ignored_legacy_aliases"], [str(legacy)]
            )
            self.assertEqual(
                [row["path"] for row in dependency["texture_dependencies"]],
                [str(canonical)],
            )
            self.assertNotIn(
                "CLUSTER_TGA_BASENAME_INVALID",
                [row["code"] for row in contract["handoff"]["errors"]],
            )

            receipt = persist_cluster_assembly_receipt(
                contract, receipt_dir=Path(temp) / "receipts"
            )
            payload = load_cluster_assembly_receipt(
                receipt, requested_spm=target
            )
            persisted = payload["cluster_assembly"]["handoff"][
                "cluster_dependencies"
            ][0]
            self.assertEqual(
                [row["path"] for row in persisted["texture_dependencies"]],
                [str(canonical)],
            )

            # A receipt produced by an older version can contain both rows and
            # the resulting false handoff error.  It remains compatible: load
            # normalizes only the duplicate alias and removes only that error.
            legacy_payload = json.loads(receipt.read_text(encoding="utf-8"))
            legacy_issue = {
                "code": "CLUSTER_TGA_BASENAME_INVALID",
                "role": persisted["role"],
                "spm": persisted["spm"],
                "details": {},
            }
            for dependency_group in (
                legacy_payload["cluster_assembly"]["dependencies"],
                legacy_payload["cluster_assembly"]["handoff"][
                    "cluster_dependencies"
                ],
            ):
                legacy_dependency = dependency_group[0]
                legacy_dependency["texture_dependencies"].append({
                    "path": str(legacy),
                    "exists": False,
                    "size": None,
                    "mtime_ns": None,
                    "sha256": None,
                })
                legacy_dependency["tga_basename_validation"].update({
                    "status": "missing",
                    "refs": [str(canonical), str(legacy)],
                    "missing": [str(legacy)],
                    "invalid": [],
                })
            legacy_payload["cluster_assembly"]["handoff"]["issues"].append(
                dict(legacy_issue)
            )
            legacy_payload["cluster_assembly"]["handoff"]["errors"].append(
                dict(legacy_issue)
            )
            receipt.write_text(json.dumps(legacy_payload), encoding="utf-8")

            upgraded = load_cluster_assembly_receipt(
                receipt, requested_spm=target
            )
            upgraded_handoff = upgraded["cluster_assembly"]["handoff"]
            self.assertNotIn(
                "CLUSTER_TGA_BASENAME_INVALID",
                [row["code"] for row in upgraded_handoff["errors"]],
            )
            self.assertEqual(
                upgraded_handoff["cluster_dependencies"][0][
                    "tga_basename_validation"
                ]["status"],
                "ok",
            )

    def test_canonical_cluster_dependency_matches_exact_raw_output_stem(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Tree_elm"
            cluster = folder / "Cluster"
            raw = cluster / "branch_elm_01.spm"
            canonical = cluster / "SK_branch_elm_01.spm"
            target = folder / "SK_Tree_elm_01.spm"
            output = cluster / "branch_elm_01.tga"
            write_spm(raw, [("1", "M_Bark_elm_01", [], [])])
            write_spm(target, [(
                "2", "M_branch_elm_01",
                ["Cluster/branch_elm_01.tga"], ("1",),
            )], mesh_ids=("1",))
            output.write_bytes(b"raw-output")
            prepare_sk(cluster, [raw.stem], dry_run=False)

            clusters = cluster_spms(folder)
            usage = cluster_material_usage([target], clusters)

            self.assertEqual(clusters, [canonical])
            self.assertIn(str(canonical).casefold(), usage)
            self.assertEqual(
                usage[str(canonical).casefold()]["source_albedo"],
                [str(output)],
            )
            self.assertNotIn("SK_branch_elm_01.tga", str(usage))

    def test_actual_hierarchy_role_gate_and_handoff_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "Tree_elm"
            cluster_dir = folder / "Cluster"
            branch = cluster_dir / "branch_elm_01.spm"
            leaf = cluster_dir / "leaf_elm_01.spm"
            leaf_side = cluster_dir / "leaf_elm_side_01.spm"
            unused = cluster_dir / "leaf_elm_02.spm"
            target = folder / "SK_Tree_elm_01.spm"
            assembly_source = folder / "Tree_elm_01.spm"

            tree_materials = [
                ("1", "M_Bark_elm_01", ["texture/T_Bark_elm_01_color.tga"], ()),
                ("2", "branch_elm_01", ["Cluster/branch_elm_01.tga"], ("1",)),
                ("3", "leaf_elm_01", ["Cluster/leaf_elm_01.tga"], ("2",)),
                ("4", "leaf_elm_side_01", ["Cluster/leaf_elm_side_01.tga"], ("3",)),
            ]
            write_spm(target, tree_materials, mesh_ids=("1", "2", "3"))
            write_spm(
                assembly_source, tree_materials, mesh_ids=("1", "2", "3"))
            write_spm(branch, [
                ("1", "Bark_elm_01", ["foreign/Nothofagus_bark.tga"], ()),
            ])
            write_spm(leaf, [
                ("1", "Bark_tree_NothofagusSolandri_01", ["foreign/Nothofagus_bark.tga"], ()),
            ])
            write_spm(leaf_side, [
                ("1", "Leaf_side", ["source/leaf.tga"], ()),
            ])
            write_spm(unused, [
                ("1", "Unused", ["source/unused.tga"], ()),
            ])

            source_refs = {}
            for spm in (branch, leaf, leaf_side):
                color = cluster_dir / f"{spm.stem}.tga"
                color.write_bytes(b"fixture")
                source_refs[str(spm).casefold()] = {
                    "spms": [str(target)],
                    "material_names": [spm.stem],
                    "material_names_by_spm": {str(target): [spm.stem]},
                    "source_refs": [str(color)],
                }

            write_ascii_fbx(
                folder / "fbx" / "Tree_elm_01.fbx",
                material_names=["branch_elm_01_Mat", "leaf_elm_01_Mat"],
                mesh_names=["branch_piece", "unrelated_leaf_mesh"],
                pairs=[("branch_elm_01_Mat", "branch_piece")],
            )
            contract = build_cluster_assembly_contract(
                folder,
                [target],
                [branch, leaf, leaf_side, unused],
                cluster_usage=source_refs,
            )

            dependencies = {
                row["name"]: row for row in contract["dependencies"]}
            children = {
                row["role"]: row for row in contract["hierarchy"]["children"]}

            self.assertEqual(
                set(dependencies),
                {"SK_branch_elm_01", "SK_leaf_elm_01", "SK_leaf_elm_side_01"},
            )
            self.assertEqual(dependencies["SK_branch_elm_01"]["decision"], "blocked")
            self.assertTrue(
                dependencies["SK_branch_elm_01"]["normalized_variants_missing"]
            )
            self.assertEqual(dependencies["SK_leaf_elm_01"]["decision"], "blocked")
            self.assertEqual(
                dependencies["SK_leaf_elm_side_01"]["role"], "leaf_side"
            )
            self.assertEqual(
                dependencies["SK_leaf_elm_side_01"]["decision"], "pass_through"
            )
            self.assertEqual(children["leaf"]["references"], [])
            self.assertEqual(children["leaf_side"]["references"], [])
            self.assertEqual(contract["canonical_bark"]["status"], "canonical")
            self.assertEqual(contract["handoff"]["status"], "blocked")
            self.assertEqual(
                contract["handoff"]["receipt_kind"],
                "pcg_cluster_assembly_handoff",
            )
            self.assertTrue(
                contract["handoff"]["separate_nanite_assembly_requested"])
            self.assertTrue(contract["handoff"]["requires_actual_fbx_revalidation"])
            self.assertEqual(
                [row["name"] for row in contract["handoff"]["roles"]],
                [
                    "SK_branch_elm_01",
                    "SK_leaf_elm_01",
                    "SK_leaf_elm_side_01",
                ],
            )
            self.assertEqual(
                len(contract["handoff"]["cluster_dependencies"]), 3)
            self.assertEqual(
                contract["handoff"]["errors"],
                contract["handoff"]["issues"],
            )
            self.assertTrue(
                contract["tree_source_identities"][0]["target_spm"]["sha256"])
            self.assertEqual(
                contract["tree_source_identities"][0]
                ["authoritative_tree_source"]["path"],
                str(assembly_source),
            )
            self.assertEqual(
                contract["handoff"]["roles"][0]["targets"][0]["spm"],
                str(assembly_source),
            )
            self.assertEqual(
                dependencies["SK_branch_elm_01"]["tga_basename_validation"]["status"],
                "ok",
            )
            wind = contract["handoff"]["skeleton_wind_contract"]
            self.assertEqual(wind["mode"], "regenerate_from_final_skeleton")
            self.assertNotIn("required_bone_count", wind)
            self.assertNotIn("production_dynamic_wind_copy", wind)

            receipt_dir = Path(temp_dir) / "tool_reports" / "cluster_assembly"
            receipt_path = persist_cluster_assembly_receipt(
                contract, receipt_dir=receipt_dir)
            first_mtime = receipt_path.stat().st_mtime_ns
            self.assertIn(
                json.loads(receipt_path.read_text(encoding="utf-8"))
                ["source_path_identity"]["sha256"][:20],
                receipt_path.name,
            )
            payload = load_cluster_assembly_receipt(
                receipt_path, requested_spm=target)
            persisted_dependencies = payload["cluster_assembly"]["handoff"][
                "cluster_dependencies"]
            self.assertTrue(
                persisted_dependencies[0]["texture_dependencies"][0]["sha256"])
            self.assertEqual(
                locate_cluster_assembly_receipt(target, receipt_dir),
                receipt_path,
            )
            self.assertEqual(
                locate_cluster_assembly_receipt(assembly_source, receipt_dir),
                receipt_path,
            )
            self.assertEqual(
                persist_cluster_assembly_receipt(contract, receipt_dir),
                receipt_path,
            )
            self.assertEqual(receipt_path.stat().st_mtime_ns, first_mtime)

            report = {"items": [{"cluster_assembly": contract}]}
            persisted = persist_cluster_assembly_receipts(report, receipt_dir)
            self.assertEqual(persisted, [str(receipt_path)])
            self.assertEqual(
                report["items"][0]["cluster_assembly_receipt"],
                str(receipt_path),
            )

            with assembly_source.open("ab") as handle:
                handle.write(b"stale")
            with self.assertRaises(ClusterAssemblyReceiptStaleError):
                locate_cluster_assembly_receipt(target, receipt_dir)

    def test_three_roles_use_stable_scope_receipts_and_arbitrary_variant_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "Tree_elm"
            target = folder / "SK_Tree_elm_01.spm"
            blend = folder / "normalized.blend"
            blend.parent.mkdir(parents=True)
            blend.write_bytes(b"blend")
            materials = [
                ("2", "branch_elm_01", [], ("10", "11")),
                ("3", "leaf_elm_01", [], ("20",)),
                ("4", "leaf_elm_side_01", [], ("30", "31", "32")),
            ]
            write_spm(
                target,
                materials,
                mesh_ids=("10", "11", "20", "30", "31", "32"),
            )
            scope_dir = folder / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            role_specs = {
                "branch": ("branch_elm_01", 2, [10, 11]),
                "leaf": ("leaf_elm_01", 3, [20]),
                "leaf_side": ("leaf_elm_side_01", 4, [30, 31, 32]),
            }
            for role, (identity, material_id, mesh_ids) in role_specs.items():
                rows = []
                for ordinal, mesh_id in enumerate(mesh_ids, 1):
                    fbx = folder / f"{role}_{ordinal:02d}.fbx"
                    fbx.write_bytes(f"{role}-{ordinal}".encode("ascii"))
                    if role == "branch":
                        skeletal_asset_name = "SK_branch_shared_01"
                    elif role == "leaf_side":
                        skeletal_asset_name = "SK_leaf_elm_side_01_01"
                    else:
                        skeletal_asset_name = f"SK_{identity}_{ordinal:02d}"
                    rows.append({
                        "source_object": f"{identity}_{ordinal:02d}",
                        "source_ordinal": ordinal,
                        "fbx": str(fbx),
                        "skeletal_asset_name": skeletal_asset_name,
                        "source_prototype_index": (
                            1 if role == "leaf_side" else ordinal
                        ),
                        "source_partition_mode": (
                            "WHOLE_MESH"
                            if role == "leaf_side"
                            else "BRANCH_COMPONENTS"
                        ),
                    })
                payload = {
                    "spm": str(target),
                    "blend_file": str(blend),
                    "material_groups": [{
                        "material": identity,
                        "material_id": material_id,
                        "mesh_ids": mesh_ids,
                        "meshes": rows,
                    }],
                }
                (scope_dir / f"scope_{role}__{target.stem}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

            # The rolling global file is deliberately stale/malformed.  Once
            # stable scope receipts exist it must not shadow them.
            (folder / "speedtree_import_manifest.json").write_text(
                json.dumps({
                    "spm": str(target),
                    "blend_file": str(blend),
                    "material_groups": [{
                        "material": "branch_elm_01",
                        "material_id": 2,
                        "mesh_ids": [10, 11],
                        "meshes": [],
                    }],
                }),
                encoding="utf-8",
            )

            self.assertEqual(
                dependency_role("SK_leaf_elm_side_01"), "leaf_side"
            )
            contracts = {
                role: _atlas_normalized_variants(
                    folder,
                    identity,
                    [target],
                    audit=audit_module,
                )
                for role, (identity, _material_id, _mesh_ids) in role_specs.items()
            }
            self.assertEqual(
                {role: len(value["variants"]) for role, value in contracts.items()},
                {"branch": 2, "leaf": 1, "leaf_side": 3},
            )
            self.assertEqual(
                {row["skeletal_asset_name"] for row in contracts["branch"]["variants"]},
                {"SK_branch_shared_01"},
            )
            side_variants = contracts["leaf_side"]["variants"]
            self.assertEqual(
                [row["plan_name"] for row in side_variants],
                [
                    "leaf_elm_side_01_01",
                    "leaf_elm_side_01_02",
                    "leaf_elm_side_01_03",
                ],
            )
            self.assertEqual(
                [row["skeletal_asset_name"] for row in side_variants],
                ["SK_leaf_elm_side_01_01"] * 3,
            )
            self.assertEqual(
                [row["source_prototype_index"] for row in side_variants],
                [1, 1, 1],
            )
            self.assertEqual(
                [row["source_partition_mode"] for row in side_variants],
                ["WHOLE_MESH"] * 3,
            )

    def test_physical_scope_propagates_normalized_bounds_and_receipts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "Tree_elm"
            target = folder / "SK_Tree_elm_01.spm"
            blend = folder / "SK_branch_elm_01.blend"
            source_spm = folder / "SK_branch_elm_01.spm"
            source_fbx = folder / "fbx" / "SK_branch_elm_01.fbx"
            plan_fbx = folder / "branch_elm_01_01.fbx"
            folder.mkdir(parents=True)
            blend.write_bytes(b"blend")
            source_spm.write_bytes(b"source-spm")
            source_fbx.parent.mkdir()
            source_fbx.write_bytes(b"source-fbx")
            plan_fbx.write_bytes(b"plan")
            write_spm(
                target,
                [("2", "branch_elm_01", [], ("10",))],
                mesh_ids=("10",),
            )
            save_target_registry(blend, [target])
            bounds = {
                "minimum": [-0.04, -0.045, -0.01],
                "maximum": [0.04, 0.045, 0.01],
                "size": [0.08, 0.09, 0.02],
                "center": [0.0, 0.0, 0.0],
            }
            capture_hash = "physical-capture-hash"
            receipt = {
                "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                "size_policy": "uniform_whole_source_physical_target_meters",
                "plan_uv_policy": "direct_physical_capture_projection",
                "direct_uv_source": (
                    "same_blender_physical_capture_projection"
                ),
                "generator_size_policy": (
                    "preserve_user_authored_leaf_and_frond_dimensions"
                ),
                "physical_capture_contract": {
                    "kind": "speedtree_cluster_physical_capture_fit",
                    "contract_sha256": capture_hash,
                },
                "physical_capture_contract_sha256": capture_hash,
                "prototypes": [{
                    "prototype_index": 1,
                    "skeletal_asset": "SK_branch_elm_01_01",
                    "normalized_bounds": bounds,
                }],
                "variants": [{
                    "card_index": 1,
                    "skeletal_asset": "SK_branch_elm_01_01",
                    "plan": "branch_elm_01_01",
                    "plan_uv_transfer": {
                        "source_3d_contract": {
                            "source_spm": str(source_spm),
                            "source_spm_sha256": file_fingerprint(
                                source_spm
                            )["sha256"],
                            "source_fbx": str(source_fbx),
                            "source_fbx_sha256": file_fingerprint(
                                source_fbx
                            )["sha256"],
                        },
                        "attachment_vertex_index": 7,
                        "attachment_vertex_uv": [0.25, 0.75],
                        "capture_attachment": {
                            "normalized_local": [0.0, 0.0, 0.0],
                        },
                    },
                }],
            }
            payload = {
                "spm": str(target),
                "blend_file": str(blend),
                "unit_probe_contract": {
                    "kind": "speedtree_fbx_spm_unit_probe",
                    "status": "verified",
                },
                "normalized_prototype_receipt": receipt,
                "material_groups": [{
                    "material": "branch_elm_01",
                    "material_id": 2,
                    "mesh_ids": [10],
                    "meshes": [{
                        "source_object": "branch_elm_01_01",
                        "source_ordinal": 1,
                        "fbx": str(plan_fbx),
                        "skeletal_asset_name": "SK_branch_elm_01_01",
                        "source_prototype_index": 1,
                        "source_partition_mode": (
                            "PER_CONNECTED_DEFORM_CLUSTER"
                        ),
                        "normalization_workflow_mode": (
                            "PHYSICAL_DIRECT_CAPTURE"
                        ),
                        "physical_capture_contract_sha256": capture_hash,
                        "normalized_bounds": json.loads(json.dumps(bounds)),
                    }],
                }],
            }
            scope_dir = folder / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            manifest_path = (
                scope_dir / f"scope_branch__{target.stem}.json"
            )
            manifest_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            contract = _atlas_normalized_variants(
                folder,
                "branch_elm_01",
                [target],
                audit=audit_module,
            )

            self.assertEqual(
                contract["variants"][0]["normalized_bounds"]["size"],
                [0.08, 0.09, 0.02],
            )
            self.assertEqual(
                contract["variants"][0]["attachment_vertex_index"],
                7,
            )
            self.assertEqual(
                contract["variants"][0]["attachment_vertex_uv"],
                [0.25, 0.75],
            )
            self.assertEqual(
                contract["production_normalization"],
                receipt,
            )
            self.assertEqual(
                contract["unit_probe_contract"]["kind"],
                "speedtree_fbx_spm_unit_probe",
            )
            self.assertEqual(
                contract["source_3d_artifacts"]["source_spm"]["path"],
                str(source_spm),
            )
            self.assertTrue(contract["target_registry"]["sha256"])

            payload["material_groups"][0]["meshes"][0][
                "normalized_bounds"
            ]["size"][0] = 0.8
            manifest_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ClusterAssemblyReceiptError,
                "bounds disagree",
            ):
                _atlas_normalized_variants(
                    folder,
                    "branch_elm_01",
                    [target],
                    audit=audit_module,
                )

            payload["material_groups"][0]["meshes"][0][
                "normalized_bounds"
            ]["size"][0] = 0.08
            manifest_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            for label, source_path in (
                ("SPM", source_spm),
                ("FBX", source_fbx),
            ):
                with self.subTest(stale_source=label):
                    original = source_path.read_bytes()
                    source_path.write_bytes(original + b"-changed")
                    with self.assertRaisesRegex(
                        ClusterAssemblyReceiptStaleError,
                        f"source {label}.*stale",
                    ):
                        _atlas_normalized_variants(
                            folder,
                            "branch_elm_01",
                            [target],
                            audit=audit_module,
                        )
                    source_path.write_bytes(original)

            source_3d_contract = dict(
                receipt["variants"][0]["plan_uv_transfer"][
                    "source_3d_contract"
                ]
            )
            del receipt["variants"][0]["plan_uv_transfer"][
                "source_3d_contract"
            ]
            manifest_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ClusterAssemblyReceiptError,
                "source 3D contract",
            ):
                _atlas_normalized_variants(
                    folder,
                    "branch_elm_01",
                    [target],
                    audit=audit_module,
                )

            receipt["variants"][0]["plan_uv_transfer"][
                "source_3d_contract"
            ] = source_3d_contract
            del receipt["variants"][0]["plan_uv_transfer"][
                "attachment_vertex_uv"
            ]
            manifest_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ClusterAssemblyReceiptError,
                "attachment metadata",
            ):
                _atlas_normalized_variants(
                    folder,
                    "branch_elm_01",
                    [target],
                    audit=audit_module,
                )

    def test_physical_scope_coverage_follows_explicit_target_registry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "Tree_elm"
            blend = folder / "SK_branch_elm_01.blend"
            first = folder / "SK_Tree_elm_01.spm"
            second = folder / "SK_Tree_elm_02.spm"
            folder.mkdir(parents=True)
            blend.write_bytes(b"blend")
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            scope_dir = folder / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            manifest = scope_dir / f"scope_branch__{first.stem}.json"
            manifest.write_text(
                json.dumps({
                    "spm": str(first),
                    "blend_file": str(blend),
                    "material_groups": [{
                        "material": "branch_elm_01",
                        "material_id": 2,
                        "mesh_ids": [10],
                        "meshes": [{}],
                    }],
                }),
                encoding="utf-8",
            )
            normalized = {
                "status": "ready",
                "material": "branch_elm_01",
                "source_blend": file_fingerprint(blend),
                "variants": [{
                    "ordinal": 1,
                    "plan_name": "branch_elm_01_01",
                    "skeletal_asset_name": "SK_branch_elm_01_01",
                    "source_prototype_index": 1,
                    "source_partition_mode": "WHOLE_MESH",
                    "plan_fbx": {"sha256": "plan"},
                    "attachment_vertex_index": 7,
                    "attachment_vertex_uv": [0.25, 0.75],
                }],
                "production_normalization": {
                    "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                },
                "source_3d_artifacts": {
                    "source_spm": {
                        "path": str(folder / "SK_branch_elm_01.spm"),
                        "sha256": "spm",
                    },
                    "source_fbx": {
                        "path": str(
                            folder / "fbx" / "SK_branch_elm_01.fbx"
                        ),
                        "sha256": "fbx",
                    },
                },
            }

            # The second target is selected for the folder audit but explicitly
            # OFF for this blend, so no scope delivery is required for it.
            save_target_registry(blend, [first])
            with mock.patch(
                "pcg_cluster_assembly_contract._normalized_variant_contract",
                return_value=normalized,
            ):
                contract = _atlas_normalized_variants(
                    folder,
                    "branch_elm_01",
                    [first, second],
                    audit=None,
                )
            self.assertEqual(
                contract["registered_target_spms"],
                [str(first.absolute())],
            )

            # Turning the exact relation ON makes the matching target receipt
            # mandatory; no asset/species-specific allowlist is involved.
            save_target_registry(blend, [first, second])
            with mock.patch(
                "pcg_cluster_assembly_contract._normalized_variant_contract",
                return_value=normalized,
            ), self.assertRaisesRegex(
                ClusterAssemblyReceiptStaleError,
                "missing current target scope",
            ):
                _atlas_normalized_variants(
                    folder,
                    "branch_elm_01",
                    [first, second],
                    audit=None,
                )

    def test_same_delivery_to_several_targets_is_one_role_contract(self):
        # A Cluster blend that is ON for more than one tree SPM writes one
        # scope manifest per target, each with that target's local Material and
        # Mesh IDs.  Those are per-target bookkeeping, not competing receipts.
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "Tree_elm"
            blend = folder / "SK_branch_elm_01.blend"
            plan_fbx = folder / "branch_elm_01_01.fbx"
            folder.mkdir(parents=True)
            blend.write_bytes(b"blend")
            plan_fbx.write_bytes(b"plan")
            scope_dir = folder / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()

            targets = []
            for index, (material_id, mesh_id) in enumerate(
                ((2, 10), (8, 21), (4, 33)), start=1
            ):
                target = folder / f"SK_Tree_elm_0{index}.spm"
                write_spm(
                    target,
                    [(str(material_id), "branch_elm_01", [], (str(mesh_id),))],
                    mesh_ids=(str(mesh_id),),
                )
                targets.append(target)
                (scope_dir / f"scope_branch__{target.stem}.json").write_text(
                    json.dumps({
                        "spm": str(target),
                        "blend_file": str(blend),
                        "material_groups": [{
                            "material": "branch_elm_01",
                            "material_id": material_id,
                            "mesh_ids": [mesh_id],
                            "meshes": [{
                                "source_object": "branch_elm_01_01",
                                "source_ordinal": 1,
                                "fbx": str(plan_fbx),
                                "skeletal_asset_name": "SK_branch_elm_01_01",
                                "source_prototype_index": 1,
                                "source_partition_mode": "WHOLE_MESH",
                            }],
                        }],
                    }),
                    encoding="utf-8",
                )

            contract = _atlas_normalized_variants(
                folder, "branch_elm_01", targets, audit=audit_module,
            )

            self.assertEqual(contract["material"], "branch_elm_01")
            self.assertEqual(
                [row["plan_name"] for row in contract["variants"]],
                ["branch_elm_01_01"],
            )

    def test_same_plan_with_diverging_pivot_metadata_reports_multiple_receipts(self):
        pivot_cases = (
            ("attachment index", (8, [0.25, 0.75])),
            ("attachment UV", (7, [0.5, 0.75])),
        )
        for label, changed_pivot in pivot_cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as temp_dir:
                    folder = Path(temp_dir) / "Tree_elm"
                    blend = folder / "SK_branch_elm_01.blend"
                    source_spm = folder / "SK_branch_elm_01.spm"
                    source_fbx = (
                        folder / "fbx" / "SK_branch_elm_01.fbx"
                    )
                    plan_fbx = folder / "branch_elm_01_01.fbx"
                    folder.mkdir(parents=True)
                    blend.write_bytes(b"blend")
                    source_spm.write_bytes(b"source-spm")
                    source_fbx.parent.mkdir()
                    source_fbx.write_bytes(b"source-fbx")
                    plan_fbx.write_bytes(b"plan")
                    scope_dir = folder / ".atlas_leaf_speedtree_scopes"
                    scope_dir.mkdir()
                    bounds = {
                        "minimum": [-0.04, -0.045, -0.01],
                        "maximum": [0.04, 0.045, 0.01],
                        "size": [0.08, 0.09, 0.02],
                        "center": [0.0, 0.0, 0.0],
                    }
                    capture_hash = "physical-capture-hash"
                    targets = []
                    for index, (vertex_index, vertex_uv) in enumerate(
                        ((7, [0.25, 0.75]), changed_pivot),
                        start=1,
                    ):
                        target = folder / f"SK_Tree_elm_0{index}.spm"
                        write_spm(
                            target,
                            [("2", "branch_elm_01", [], ("10",))],
                            mesh_ids=("10",),
                        )
                        targets.append(target)
                        receipt = {
                            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                            "physical_capture_contract": {
                                "kind": "speedtree_cluster_physical_capture_fit",
                                "contract_sha256": capture_hash,
                            },
                            "physical_capture_contract_sha256": capture_hash,
                            "prototypes": [{
                                "prototype_index": 1,
                                "skeletal_asset": "SK_branch_elm_01_01",
                                "normalized_bounds": bounds,
                            }],
                            "variants": [{
                                "card_index": 1,
                                "skeletal_asset": "SK_branch_elm_01_01",
                                "plan": "branch_elm_01_01",
                                "plan_uv_transfer": {
                                    "source_3d_contract": {
                                        "source_spm": str(source_spm),
                                        "source_spm_sha256": file_fingerprint(
                                            source_spm
                                        )["sha256"],
                                        "source_fbx": str(source_fbx),
                                        "source_fbx_sha256": file_fingerprint(
                                            source_fbx
                                        )["sha256"],
                                    },
                                    "attachment_vertex_index": vertex_index,
                                    "attachment_vertex_uv": vertex_uv,
                                    "capture_attachment": {
                                        "normalized_local": [0.0, 0.0, 0.0],
                                    },
                                },
                            }],
                        }
                        payload = {
                            "spm": str(target),
                            "blend_file": str(blend),
                            "unit_probe_contract": {
                                "kind": "speedtree_fbx_spm_unit_probe",
                                "status": "verified",
                            },
                            "normalized_prototype_receipt": receipt,
                            "material_groups": [{
                                "material": "branch_elm_01",
                                "material_id": 2,
                                "mesh_ids": [10],
                                "meshes": [{
                                    "source_object": "branch_elm_01_01",
                                    "source_ordinal": 1,
                                    "fbx": str(plan_fbx),
                                    "skeletal_asset_name": (
                                        "SK_branch_elm_01_01"
                                    ),
                                    "source_prototype_index": 1,
                                    "source_partition_mode": (
                                        "PER_CONNECTED_DEFORM_CLUSTER"
                                    ),
                                    "normalization_workflow_mode": (
                                        "PHYSICAL_DIRECT_CAPTURE"
                                    ),
                                    "physical_capture_contract_sha256": (
                                        capture_hash
                                    ),
                                    "normalized_bounds": bounds,
                                }],
                            }],
                        }
                        (
                            scope_dir
                            / f"scope_branch__{target.stem}.json"
                        ).write_text(
                            json.dumps(payload),
                            encoding="utf-8",
                        )

                    save_target_registry(blend, targets)
                    with self.assertRaisesRegex(
                        ClusterAssemblyReceiptError,
                        "multiple current receipts",
                    ):
                        _atlas_normalized_variants(
                            folder,
                            "branch_elm_01",
                            targets,
                            audit=audit_module,
                        )

    def test_diverging_plan_delivery_still_reports_multiple_receipts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "Tree_elm"
            blend = folder / "SK_branch_elm_01.blend"
            folder.mkdir(parents=True)
            blend.write_bytes(b"blend")
            scope_dir = folder / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()

            targets = []
            for index, plan in enumerate(
                ("branch_elm_01_01", "branch_elm_01_09"), start=1
            ):
                target = folder / f"SK_Tree_elm_0{index}.spm"
                write_spm(
                    target,
                    [("2", "branch_elm_01", [], ("10",))],
                    mesh_ids=("10",),
                )
                targets.append(target)
                plan_fbx = folder / f"{plan}.fbx"
                plan_fbx.write_bytes(plan.encode("utf-8"))
                (scope_dir / f"scope_branch__{target.stem}.json").write_text(
                    json.dumps({
                        "spm": str(target),
                        "blend_file": str(blend),
                        "material_groups": [{
                            "material": "branch_elm_01",
                            "material_id": 2,
                            "mesh_ids": [10],
                            "meshes": [{
                                "source_object": plan,
                                "source_ordinal": 1,
                                "fbx": str(plan_fbx),
                                "skeletal_asset_name": f"SK_{plan}",
                                "source_prototype_index": 1,
                                "source_partition_mode": "WHOLE_MESH",
                            }],
                        }],
                    }),
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(
                ClusterAssemblyReceiptError, "multiple current receipts"
            ):
                _atlas_normalized_variants(
                    folder, "branch_elm_01", targets, audit=audit_module,
                )

    def test_composite_side_contract_preserves_all_deform_subparts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "Tree_elm"
            target = folder / "SK_Tree_elm_01.spm"
            blend = folder / "SK_leaf_elm_side_01.blend"
            folder.mkdir(parents=True)
            blend.write_bytes(b"blend")
            write_spm(
                target,
                [("4", "leaf_elm_side_01", [], ("30", "31", "32"))],
                mesh_ids=("30", "31", "32"),
            )
            scope_dir = folder / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            composite_parts = [
                {
                    "subpart_index": index,
                    "skeletal_asset_name": f"SK_leaf_elm_side_01_{index:02d}",
                    "source_bone": f"Bone_{index}_Start",
                    "endpoint_bone": f"Bone_{index}_End",
                    "subpart_to_card_matrix": [
                        [1.0, 0.0, 0.0, float(index)],
                        [0.0, 1.0, 0.0, 0.0],
                        [0.0, 0.0, 1.0, 0.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ],
                }
                for index in range(1, 13)
            ]
            rows = []
            for ordinal, mesh_id in enumerate((30, 31, 32), 1):
                fbx = folder / f"side_{ordinal:02d}.fbx"
                fbx.write_bytes(f"side-{ordinal}".encode("ascii"))
                rows.append({
                    "source_object": f"leaf_elm_side_01_{ordinal:02d}",
                    "source_ordinal": ordinal,
                    "fbx": str(fbx),
                    "skeletal_asset_name": "SK_leaf_elm_side_01_01",
                    "source_prototype_index": 1,
                    "source_partition_mode": "COMPOSITE_PER_DEFORM_ROOT",
                    "composite_parts": composite_parts,
                })
            payload = {
                "spm": str(target),
                "blend_file": str(blend),
                "material_groups": [{
                    "material": "leaf_elm_side_01",
                    "material_id": 4,
                    "mesh_ids": [30, 31, 32],
                    "meshes": rows,
                }],
            }
            (scope_dir / f"scope_side__{target.stem}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            contract = _atlas_normalized_variants(
                folder,
                "leaf_elm_side_01",
                [target],
                audit=audit_module,
            )

            self.assertEqual(
                contract["contract"],
                "atlas_normalized_plan_composite_skeletal_pair_v2",
            )
            self.assertEqual(len(contract["variants"]), 3)
            for variant in contract["variants"]:
                self.assertEqual(len(variant["composite_parts"]), 12)
                self.assertEqual(
                    [row["subpart_index"] for row in variant["composite_parts"]],
                    list(range(1, 13)),
                )

    def test_latest_current_overlapping_receipt_wins_and_history_is_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt_dir = root / "receipts"
            target = root / "SK_Tree_elm_01.spm"
            source = root / "Tree_elm_01.spm"
            extra_target = root / "SK_Tree_elm_02.spm"
            extra_source = root / "Tree_elm_02.spm"
            for path, payload in (
                (target, b"target-01"),
                (source, b"source-01"),
                (extra_target, b"target-02"),
                (extra_source, b"source-02"),
            ):
                path.write_bytes(payload)

            first_contract = {
                "folder": str(root),
                "tree_source_identities": [{
                    "target_spm": file_fingerprint(target),
                    "authoritative_tree_source": file_fingerprint(source),
                }],
                "dependencies": [],
                "handoff": {"cluster_dependencies": []},
            }
            second_contract = {
                **first_contract,
                "tree_source_identities": [
                    *first_contract["tree_source_identities"],
                    {
                        "target_spm": file_fingerprint(extra_target),
                        "authoritative_tree_source": file_fingerprint(extra_source),
                    },
                ],
            }
            first = persist_cluster_assembly_receipt(
                first_contract, receipt_dir=receipt_dir
            )
            second = persist_cluster_assembly_receipt(
                second_contract, receipt_dir=receipt_dir
            )
            os.utime(first, ns=(1_000_000_000, 1_000_000_000))
            os.utime(second, ns=(2_000_000_000, 2_000_000_000))

            resolution = cluster_assembly_receipt_resolution(
                target, receipt_dir
            )

            self.assertEqual(Path(resolution["selected_receipt"]), second)
            self.assertEqual(
                resolution["superseded_current_receipts"], [str(first)]
            )
            self.assertEqual(
                locate_cluster_assembly_receipt(target, receipt_dir), second
            )

    def test_persisted_receipt_tracks_nested_physical_source_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Tree_elm"
            receipt_dir = Path(temp_dir) / "receipts"
            target = root / "SK_Tree_elm_01.spm"
            source = root / "Tree_elm_01.spm"
            source_spm = root / "Cluster" / "SK_branch_elm_01.spm"
            source_fbx = root / "Cluster" / "fbx" / "SK_branch_elm_01.fbx"
            source_blend = root / "Cluster" / "SK_branch_elm_01.blend"
            for path, content in (
                (target, b"target"),
                (source, b"source"),
                (source_spm, b"cluster-spm"),
                (source_fbx, b"cluster-fbx"),
                (source_blend, b"cluster-blend"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            registry = save_target_registry(source_blend, [target])
            source_contract = {
                "source_spm": str(source_spm),
                "source_spm_sha256": file_fingerprint(
                    source_spm
                )["sha256"],
                "source_fbx": str(source_fbx),
                "source_fbx_sha256": file_fingerprint(
                    source_fbx
                )["sha256"],
            }
            normalized = {
                "manifest": file_fingerprint(source_fbx),
                "source_blend": file_fingerprint(source_blend),
                "source_3d_artifacts": {
                    "source_spm": file_fingerprint(source_spm),
                    "source_fbx": file_fingerprint(source_fbx),
                },
                "target_registry": file_fingerprint(
                    registry["registry_path"]
                ),
                "production_normalization": {
                    "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                    "variants": [{
                        "plan_uv_transfer": {
                            "source_3d_contract": source_contract,
                        },
                    }],
                },
                "variants": [{
                    "plan_fbx": file_fingerprint(source_fbx),
                }],
            }
            dependency = {
                "spm_fingerprint": file_fingerprint(source_spm),
                "normalized_variants": normalized,
            }
            contract = {
                "folder": str(root),
                "tree_source_identities": [{
                    "target_spm": file_fingerprint(target),
                    "authoritative_tree_source": file_fingerprint(source),
                }],
                "dependencies": [dependency],
                "handoff": {"cluster_dependencies": [dependency]},
            }
            receipt = persist_cluster_assembly_receipt(
                contract,
                receipt_dir=receipt_dir,
            )
            load_cluster_assembly_receipt(receipt, requested_spm=target)

            # Old persisted receipts copied the production normalization but
            # omitted the extracted source/registry artifacts.  The nested
            # provenance must still make a changed physical source stale.
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            persisted_variants = payload["cluster_assembly"]["handoff"][
                "cluster_dependencies"
            ][0]["normalized_variants"]
            del persisted_variants["source_3d_artifacts"]
            del persisted_variants["target_registry"]
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            before = source_fbx.stat()
            replacement = b"changed-fbx"
            self.assertEqual(len(replacement), before.st_size)
            source_fbx.write_bytes(replacement)
            os.utime(
                source_fbx,
                ns=(before.st_atime_ns, before.st_mtime_ns),
            )
            with self.assertRaisesRegex(
                ClusterAssemblyReceiptStaleError,
                "physical source FBX.*stale",
            ):
                load_cluster_assembly_receipt(
                    receipt,
                    requested_spm=target,
                )

    def test_persisted_export_bundle_uses_content_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Tree_elm"
            receipt_dir = Path(temp_dir) / "receipts"
            target = root / "SK_Tree_elm_01.spm"
            source = root / "Tree_elm_01.spm"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"target-spm")
            source.write_bytes(b"source-spm")

            export_dir = root / "fbx"
            export_dir.mkdir()
            exports = {
                "fbx": export_dir / "Tree_elm_01.fbx",
                "xml": export_dir / "Tree_elm_01.xml",
                "stmat": export_dir / "Tree_elm_01.stmat",
            }
            original = {
                "fbx": b"fbx-content",
                "xml": b"<xml />",
                "stmat": b"<SpeedTreeMaterials />",
            }
            for artifact, path in exports.items():
                path.write_bytes(original[artifact])

            contract = {
                "folder": str(root),
                "tree_source_identities": [{
                    "target_spm": file_fingerprint(target),
                    "authoritative_tree_source": file_fingerprint(source),
                }],
                "dependencies": [],
                "handoff": {
                    "cluster_dependencies": [],
                    "roles": [{
                        "role": "branch",
                        "targets": [{
                            "spm": str(source),
                            "export_bundle": {
                                artifact: file_fingerprint(
                                    path, hash_content=False
                                )
                                for artifact, path in exports.items()
                            },
                        }],
                    }],
                },
            }
            receipt = persist_cluster_assembly_receipt(
                contract, receipt_dir=receipt_dir
            )
            payload = load_cluster_assembly_receipt(
                receipt, requested_spm=target
            )
            persisted_bundle = payload["cluster_assembly"]["handoff"][
                "roles"
            ][0]["targets"][0]["export_bundle"]
            self.assertTrue(all(
                persisted_bundle[artifact]["sha256"]
                for artifact in exports
            ))

            for path in exports.values():
                stat = path.stat()
                os.utime(
                    path,
                    ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
                )
            load_cluster_assembly_receipt(receipt, requested_spm=target)

            for artifact, path in exports.items():
                with self.subTest(artifact=artifact):
                    path.write_bytes(original[artifact] + b"-changed")
                    with self.assertRaises(
                        ClusterAssemblyReceiptStaleError
                    ):
                        load_cluster_assembly_receipt(
                            receipt, requested_spm=target
                        )
                    path.write_bytes(original[artifact])
                    stat = path.stat()
                    os.utime(
                        path,
                        ns=(
                            stat.st_atime_ns,
                            stat.st_mtime_ns + 1_000_000,
                        ),
                    )
                    load_cluster_assembly_receipt(
                        receipt, requested_spm=target
                    )

    def test_gui_rows_keep_each_cluster_spm_individually_visible(self):
        gui = load_gui_module()
        rows = gui.cluster_hierarchy_rows({
            "cluster_assembly": {
                "hierarchy": {
                    "name": "Cluster",
                    "path": r"D:\Tree_elm\Cluster",
                    "children": [{
                        "role": "leaf",
                        "name": "leaf_elm_01",
                        "source_spm": r"D:\Tree_elm\Cluster\leaf_elm_01.spm",
                        "decision": "pass_through",
                        "references": [{"name": "leaf_elm_side_01"}],
                    }],
                },
                "dependencies": [{
                    "role": "leaf",
                    "name": "leaf_elm_01",
                    "source_materials": [{"material_name": "Leaf Elm"}],
                    "source_mesh_ids": ["1", "2"],
                    "texture_dependencies": [{"path": "leaf_elm_01.tga"}],
                }],
                "canonical_bark": {"status": "canonical"},
                "handoff": {"status": "pass_through"},
            },
        })

        self.assertEqual(
            [row["kind"] for row in rows],
            ["cluster", "cluster_spm", "cluster_spm"],
        )
        self.assertEqual(rows[0]["name"], "Cluster")
        self.assertEqual(rows[1]["name"], "leaf_elm_01")
        self.assertEqual(rows[2]["name"], "leaf_elm_side_01")
        self.assertEqual(rows[1]["materials"], "material 1 · mesh 2")
        self.assertEqual(rows[1]["textures"], "Cluster 출력 TGA 연결 1장")

    def test_gui_rows_show_generic_bush_cluster_without_assembly_role(self):
        gui = load_gui_module()
        source = r"D:\Tree\bush_Silky_Dogwood\Cluster\cluster_Silky_Dogwood_01.spm"
        rows = gui.cluster_hierarchy_rows({
            "folder": r"D:\Tree\bush_Silky_Dogwood",
            "cluster_source_rows": [{
                "name": "cluster_Silky_Dogwood_01",
                "source_spm": source,
                "referenced": True,
                "cluster_output_textures": [
                    r"D:\Tree\bush_Silky_Dogwood\Cluster\cluster_Silky_Dogwood_01.tga",
                ],
                "missing_cluster_output_textures": [
                    r"D:\Tree\bush_Silky_Dogwood\Cluster\cluster_Silky_Dogwood_01.tga",
                ],
                "assembly_role": None,
                "assembly_decision": None,
            }],
            "cluster_assembly": {
                "hierarchy": {"name": "Cluster", "path": str(Path(source).parent)},
                "dependencies": [],
                "handoff": {"status": "pass_through"},
            },
        })

        self.assertEqual([row["name"] for row in rows], [
            "Cluster", "cluster_Silky_Dogwood_01",
        ])
        self.assertEqual(rows[1]["role"], "")
        self.assertEqual(
            rows[1]["textures"],
            "Cluster 출력 TGA 연결 1장 · 누락 1장",
        )
        self.assertFalse(hasattr(gui, "selected_cluster_m_targets"))

    def test_saved_pcg_report_is_not_the_default_inventory_filter(self):
        gui = load_gui_module()
        self.assertFalse(gui.DEFAULT_USE_PCG_TARGETS)

    def test_gui_copy_uses_exact_cluster_child_registry(self):
        gui = load_gui_module()
        source = Path(
            r"D:\Tree\Tree_elm\Cluster\branch_elm_01.spm"
        )

        class Root:
            clipboard = "stale-parent-value"

            def clipboard_clear(self):
                self.clipboard = ""

            def clipboard_append(self, value):
                self.clipboard += value

            def update_idletasks(self):
                return None

        class Tree:
            @staticmethod
            def selection():
                return ("cluster-child",)

        class Status:
            value = ""

            def set(self, value):
                self.value = value

        app = gui.App.__new__(gui.App)
        app.root = Root()
        app.tree = Tree()
        app.status_var = Status()
        app.row_copy_paths = {"cluster-child": [source]}

        self.assertEqual(app.copy_selected_paths(), "break")
        self.assertEqual(app.root.clipboard, str(source.resolve()))
        self.assertIn("1개", app.status_var.value)

    def test_cluster_legacy_output_is_normalized_to_canonical_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            raw = cluster / "branch_elm_01.spm"
            canonical = cluster / "SK_branch_elm_01.spm"
            refs = [
                "branch_elm_01.tga",
                "branch_elm_01_Opacity.tga",
            ]
            write_spm(raw, [("5", "Bark_elm_01", refs, [])])

            preview = prepare_sk(
                cluster, ["branch_elm_01"], dry_run=True
            )["targets"][0]
            self.assertEqual(preview["pair_status"], "normalization_ready")
            self.assertEqual(Path(preview["canonical_spm"]), canonical)
            self.assertEqual(Path(preview["mirror_spm"]), raw)
            self.assertEqual(Path(preview["would_create"]), canonical)
            self.assertFalse(preview["would_publish"])
            self.assertTrue(preview["would_normalize_output_name"])
            self.assertFalse(canonical.exists())

            result = prepare_sk(
                cluster, ["branch_elm_01"], dry_run=False
            )["targets"][0]

            self.assertEqual(result["status"], "prepared")
            self.assertTrue(canonical.is_file())
            self.assertNotEqual(canonical.read_bytes(), raw.read_bytes())
            self.assertEqual(m_prefix_plan(canonical), [])
            self.assertTrue(m_prefix_plan(raw))
            self.assertEqual(
                extract_material_image_refs(raw)[0]["refs"], refs
            )
            self.assertEqual(
                inspect_cluster_spm_pair(canonical)["status"], "current"
            )

    def test_cluster_canonical_edit_never_republishes_legacy_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            raw = cluster / "leaf_elm_01.spm"
            canonical = cluster / "SK_leaf_elm_01.spm"
            write_spm(raw, [("5", "Leaf_elm_01", ["leaf_elm_01.tga"], [])])
            prepare_sk(cluster, ["leaf_elm_01"], dry_run=False)
            raw_before = raw.read_bytes()

            write_spm(canonical, [(
                "5", "M_leaf_elm_01_v2", ["leaf_elm_01.tga"], []
            )])
            self.assertEqual(
                inspect_cluster_spm_pair(canonical)["status"],
                "current",
            )
            preview = prepare_sk(
                cluster, ["leaf_elm_01"], dry_run=True
            )["targets"][0]
            self.assertFalse(preview["would_publish"])

            prepare_sk(cluster, ["leaf_elm_01"], dry_run=False)

            self.assertEqual(raw.read_bytes(), raw_before)
            self.assertNotEqual(canonical.read_bytes(), raw.read_bytes())
            self.assertEqual(
                inspect_cluster_spm_pair(canonical)["status"], "current"
            )

    def test_cluster_legacy_drift_is_ignored_without_reverse_or_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            raw = cluster / "branch_elm_01.spm"
            canonical = cluster / "SK_branch_elm_01.spm"
            write_spm(raw, [("5", "Bark_elm_01", [], [])])
            prepare_sk(cluster, ["branch_elm_01"], dry_run=False)
            canonical_before = canonical.read_bytes()

            write_spm(raw, [("5", "M_raw_independent_edit", [], [])])
            raw_before = raw.read_bytes()
            preview = prepare_sk(
                cluster, ["branch_elm_01"], dry_run=True
            )["targets"][0]
            result = prepare_sk(
                cluster, ["branch_elm_01"], dry_run=False
            )["targets"][0]

            self.assertEqual(preview["pair_status"], "current")
            self.assertEqual(preview["status"], "up_to_date")
            self.assertEqual(result["status"], "up_to_date")
            self.assertEqual(canonical.read_bytes(), canonical_before)
            self.assertEqual(raw.read_bytes(), raw_before)

    def test_cluster_detached_collision_gets_deterministic_old_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            raw = cluster / "branch_elm_01.spm"
            canonical = cluster / "SK_branch_elm_01.spm"
            write_spm(raw, [
                ("1", "Bark_elm_01", ["legacy_bark.tga"], []),
                ("2", "M_Bark_elm_01", ["current_bark.tga"], []),
                ("3", "M_Bark_elm_01_old", ["older_bark.tga"], []),
            ], active_material_ids=("2",))
            before = {
                row["material_id"]: list(row["refs"])
                for row in extract_material_image_refs(raw)
            }

            preview = prepare_sk(
                cluster, ["branch_elm_01"], dry_run=True
            )["targets"][0]
            result = prepare_sk(
                cluster, ["branch_elm_01"], dry_run=False
            )["targets"][0]

            self.assertEqual(
                dict(preview["patch"]["renames"])["Bark_elm_01"],
                "M_Bark_elm_01_old_02",
            )
            self.assertEqual(result["status"], "prepared")
            after_rows = extract_material_image_refs(canonical)
            after_names = [row["material_name"] for row in after_rows]
            self.assertEqual(len(after_names), len(set(map(str.casefold, after_names))))
            self.assertIn("M_Bark_elm_01", after_names)
            self.assertIn("M_Bark_elm_01_old_02", after_names)
            self.assertEqual(
                {row["material_id"]: list(row["refs"]) for row in after_rows},
                before,
            )

    def test_cluster_two_active_materials_for_one_name_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            raw = cluster / "branch_elm_01.spm"
            write_spm(raw, [
                ("1", "Bark_elm_01", ["first.tga"], []),
                ("2", "M_Bark_elm_01", ["second.tga"], []),
            ], active_material_ids=("1", "2"))
            before = raw.read_bytes()

            preview = prepare_sk(
                cluster, ["branch_elm_01"], dry_run=True
            )["targets"][0]

            self.assertEqual(preview["status"], "blocked")
            self.assertEqual(
                preview["material_name_conflicts"][0]["type"],
                "active_canonical_collision",
            )
            self.assertEqual(raw.read_bytes(), before)
            self.assertFalse((cluster / "SK_branch_elm_01.spm").exists())

    def test_gui_has_no_separate_cluster_prepare_button_or_public_helper(self):
        gui = load_gui_module()

        self.assertFalse(hasattr(gui, "prepare_cluster_m_prefix"))
        self.assertFalse(hasattr(gui, "selected_cluster_m_targets"))
        self.assertFalse(hasattr(gui.App, "start_cluster_m_prefix"))
        source = (TOOL_DIR / "pcg_texture_gui.pyw").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("btn_cluster_m", source)
        self.assertNotIn("①-C", source)

    @unittest.skipUnless(
        REAL_ELM_CLUSTER_BRANCH.is_file() and REAL_ELM_CLUSTER_LEAF.is_file(),
        "Tree Elm Cluster branch/leaf sources unavailable",
    )
    def test_real_elm_cluster_prepare_keeps_active_names_and_asset_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            cluster.mkdir(parents=True)
            for real_source in (
                REAL_ELM_CLUSTER_BRANCH, REAL_ELM_CLUSTER_LEAF
            ):
                with self.subTest(source=real_source.name):
                    raw = cluster / real_source.name
                    shutil.copy2(real_source, raw)
                    canonical = cluster / f"SK_{real_source.name}"
                    before_rows = extract_material_image_refs(raw)
                    before_payload = {
                        row["material_id"]: {
                            "refs": list(row["refs"]),
                            "cutout_mesh_ids": list(row["cutout_mesh_ids"]),
                        }
                        for row in before_rows
                    }
                    active_ids = set(active_material_ids(raw))
                    active_names = {
                        row["material_id"]: row["material_name"]
                        for row in before_rows
                        if row["material_id"] in active_ids
                    }

                    plan = dict(cluster_material_rename_plan(raw))
                    result = prepare_sk(
                        cluster, [raw.stem], dry_run=False
                    )["targets"][0]
                    after_rows = extract_material_image_refs(canonical)
                    after_names = [row["material_name"] for row in after_rows]
                    after_by_id = {
                        row["material_id"]: row for row in after_rows
                    }

                    self.assertEqual(result["status"], "prepared")
                    self.assertEqual(
                        len(after_names), len(set(map(str.casefold, after_names)))
                    )
                    self.assertEqual(
                        {
                            material_id: after_by_id[material_id]["material_name"]
                            for material_id in active_names
                        },
                        active_names,
                    )
                    self.assertEqual(
                        {
                            material_id: {
                                "refs": list(row["refs"]),
                                "cutout_mesh_ids": list(row["cutout_mesh_ids"]),
                            }
                            for material_id, row in after_by_id.items()
                        },
                        before_payload,
                    )
                    self.assertEqual(canonical.read_bytes(), raw.read_bytes())
                    if real_source == REAL_ELM_CLUSTER_BRANCH:
                        before_names = {
                            row["material_name"] for row in before_rows
                        }
                        if "Bark_elm_01" in before_names:
                            self.assertEqual(
                                plan["Bark_elm_01"], "M_Bark_elm_01_old"
                            )
                            self.assertEqual(
                                plan["Leaf_elm_01"], "M_Leaf_elm_01"
                            )
                        else:
                            self.assertIn("M_Bark_elm_01_old", before_names)
                            self.assertIn("M_Leaf_elm_01", before_names)
                            self.assertEqual(plan, {})

    def test_blender_rows_expose_real_file_and_connected_spm(self):
        gui = load_gui_module()
        blend = r"D:\OneDrive\Forestportfolio\02_nature\Tree\atlas\M_leaf_Clover.blend"
        spm = r"D:\OneDrive\Forestportfolio\02_nature\Weed\weed_Clover\SK_weed_Clover.spm"
        item = {
            "leaf_mesh_sources": [{
                "atlas_blends": [blend],
                "targets": [{
                    "spm": spm,
                    "generator_connection_complete": True,
                }],
            }],
        }

        rows = gui.blender_connection_rows(item)

        self.assertEqual([str(row["blend"]) for row in rows], [blend])
        self.assertEqual([str(row["spms"][0]["spm"]) for row in rows], [spm])
        self.assertTrue(rows[0]["spms"][0]["connected"])
        self.assertEqual(
            gui.blender_connection_summary(rows[0]),
            "연결 SPM 1개 · 연결 완료 ✓",
        )
        self.assertEqual(
            gui.blender_connection_overview(item),
            "blend 1개 · 연결 완료 ✓",
        )

    def test_hidden_stale_binding_does_not_fail_visible_ready_atlas(self):
        spm = Path("Tree_elm") / "SK_Tree_elm_01.spm"
        blend = Path("atlas") / "M_leaf_elm_atlas_01.blend"
        materials = [{
            "material_id": "5",
            "material_name": "M_leaf_elm_atlas_01",
            "cutout_mesh_ids": ["11"],
            "managed_leaf_output": True,
        }]
        bindings = [
            {"material_id": "5", "mesh_id": "11", "visible": True},
            {"material_id": "5", "mesh_id": "99", "visible": False},
        ]
        registry = {
            "m_leaf_elm_atlas_01": {
                "base": "M_leaf_elm_atlas_01",
                "live_blends": [str(blend)],
            }
        }
        with mock.patch(
                "pcg_texture_audit._existing_atlas_registry",
                return_value=registry), mock.patch(
                    "pcg_texture_audit.extract_material_image_refs",
                    return_value=materials), mock.patch(
                        "pcg_texture_audit.mesh_asset_ids",
                        return_value={"11"}), mock.patch(
                            "pcg_texture_audit.leaf_generator_bindings",
                            return_value=bindings):
            rows = current_leaf_atlas_inventory(
                Path("Tree_elm"), {"atlas_root": "atlas"}, [spm])

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["export_participating"])
        self.assertEqual(rows[0]["visible_binding_count"], 1)
        self.assertEqual(rows[0]["visible_ready_binding_count"], 1)
        self.assertEqual(rows[0]["ready_binding_count"], 1)
        self.assertTrue(rows[0]["generator_connection_complete"])

    def test_cluster_helper_click_selects_it_and_clears_stale_parent_checks(self):
        gui = load_gui_module()

        class Tree:
            selected = ()
            focused = None

            @staticmethod
            def identify_region(_x, _y):
                return "tree"

            @staticmethod
            def identify_row(_y):
                return "cluster-child"

            def selection_set(self, iid):
                self.selected = (iid,)

            def focus(self, iid):
                self.focused = iid

            @staticmethod
            def focus_set():
                return None

        class Checked:
            cleared = False

            def set_all(self, checked):
                self.cleared = not checked

        app = gui.App.__new__(gui.App)
        app._busy = False
        app.tree = Tree()
        app.items = {"parent": {"checked": True}}
        app.row_copy_paths = {"cluster-child": [Path("branch_elm_01.spm")]}
        app.checked_rows = Checked()
        app.target_checked_rows = Checked()
        app._update_step3_button = lambda: None
        event = type("Event", (), {"x": 0, "y": 0})()

        self.assertEqual(app._on_click(event), "break")
        self.assertEqual(app.tree.selected, ("cluster-child",))
        self.assertEqual(app.tree.focused, "cluster-child")
        self.assertTrue(app.checked_rows.cleared)
        self.assertTrue(app.target_checked_rows.cleared)


    def test_inactive_bark_like_material_does_not_require_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Tree_elm"
            target = folder / "SK_Tree_elm_01.spm"
            cluster = folder / "Cluster" / "SK_branch_elm_01.spm"
            write_spm(target, [
                ("1", "M_Bark_elm_01",
                 ["texture/T_Bark_elm_01_Color.tga"], ()),
            ])
            write_spm(
                cluster,
                [
                    ("1", "M_Bark_elm_01",
                     ["texture/T_Bark_elm_01_Color.tga"], ()),
                    ("9", "M_Mossy_Bark_legacy",
                     ["foreign/Other_Bark_Color.tga"], ()),
                ],
                active_material_ids=("1",),
            )

            contract = _canonical_bark_contract(
                audit_module,
                folder,
                [target],
                [{"source_spm": cluster, "spm": cluster}],
            )

            self.assertEqual(contract["status"], "canonical")
            self.assertEqual(
                [row["material_id"]
                 for row in contract["cluster_bark_sources"]],
                ["1"],
            )
            self.assertEqual(
                {row["replacement"]
                 for row in contract["cluster_bark_sources"]},
                {"not_required"},
            )

    def test_current_isolated_bark_capture_satisfies_production_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Tree_elm"
            cluster_dir = folder / "Cluster"
            output_spm = cluster_dir / "SK_branch_elm_01.spm"
            isolated_root = cluster_dir / ".sk_batch_isolated_bark" / "sig"
            isolated_spm = isolated_root / "Tree_elm" / "Cluster" / output_spm.name
            isolated_fbx = (
                isolated_spm.parent / "fbx" / f"{isolated_spm.stem}.fbx"
            )
            blend = cluster_dir / "SK_branch_elm_01.blend"
            target = folder / "SK_Tree_elm_01.spm"
            for path, content in (
                (isolated_spm, b"isolated-spm"),
                (isolated_fbx, b"isolated-fbx"),
                (blend, b"source-blend"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            write_spm(
                output_spm,
                [(
                    "1",
                    "M_bark_common_end_01",
                    ["foreign/common_bark_color.tga"],
                    ("1",),
                )],
                mesh_ids=("1",),
                active_material_ids=("1",),
            )
            write_spm(
                target,
                [(
                    "1",
                    "M_Bark_elm_01",
                    ["texture/T_Bark_elm_01_color.tga"],
                    ("1",),
                )],
                mesh_ids=("1",),
            )
            manifest_path = isolated_root / "bark_normalization_manifest.json"
            manifest = {
                "kind": "cluster_isolated_canonical_bark_source",
                "status": "ready",
                "source_spm": str(output_spm),
                "source_spm_sha256": file_fingerprint(output_spm)["sha256"],
                "speedtree_spm": str(isolated_spm),
                "isolated_spm_sha256": file_fingerprint(isolated_spm)["sha256"],
                "production_source_mutated": False,
                "normalization": {
                    "canonical_material": "M_bark_elm_01",
                },
            }
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            pipeline_path = (
                cluster_dir
                / "reports"
                / "SK_branch_elm_01_speedtree_repair_pipeline_report_codex.json"
            )
            pipeline_path.parent.mkdir(parents=True)
            pipeline = {
                "source_blend_identity": file_fingerprint(blend),
                "speedtree_live_source_identity": {
                    "spm": file_fingerprint(output_spm),
                },
                "cluster_bark_source_resolution": {
                    "status": "ready",
                    "manifest": file_fingerprint(manifest_path),
                    "source_spm": file_fingerprint(output_spm),
                    "speedtree_spm": file_fingerprint(isolated_spm),
                    "canonical_material": "M_bark_elm_01",
                    "production_source_mutated": False,
                },
                "cluster_bark_export_validation": {
                    "status": "ready_for_downstream_blender_mapping",
                    "canonical_material": "M_bark_elm_01",
                    "output_materials": ["M_bark_common_end_01"],
                    "fbx": {"path": str(isolated_fbx)},
                    "material_slot_propagated": True,
                    "texture_set_propagated": True,
                    "uv_preserved": True,
                    "production_sources_mutated": False,
                },
            }
            pipeline_path.write_text(
                json.dumps(pipeline),
                encoding="utf-8",
            )
            normalized = {
                "source_blend": file_fingerprint(blend),
                "source_3d_artifacts": {
                    "source_spm": file_fingerprint(isolated_spm),
                    "source_fbx": file_fingerprint(isolated_fbx),
                },
                "production_normalization": {
                    "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                },
            }

            with mock.patch(
                "pcg_cluster_bark_normalization."
                "validate_canonical_bark_export_bundle",
                return_value={
                    "status": "ready_for_downstream_blender_mapping",
                    "canonical_material": "M_bark_elm_01",
                    "output_materials": ["M_bark_common_end_01"],
                    "fbx": {"path": str(isolated_fbx)},
                    "material_slot_propagated": True,
                    "texture_set_propagated": True,
                    "uv_preserved": True,
                    "production_sources_mutated": False,
                },
            ):
                _validate_normalized_source_dependency(
                    normalized,
                    output_spm,
                )
            self.assertEqual(
                normalized["isolated_bark_capture"]["status"],
                "validated",
            )

            bark = _canonical_bark_contract(
                audit_module,
                folder,
                [target],
                [{
                    "source_spm": str(output_spm),
                    "spm": str(output_spm),
                    "output_spm": str(output_spm),
                    "normalized_variants": normalized,
                }],
            )
            self.assertEqual(bark["status"], "canonical")
            self.assertEqual(
                bark["cluster_bark_sources"][0]["replacement"],
                "isolated_capture_validated",
            )

    def test_same_role_secondary_is_not_bound_to_primary_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Tree_elm"
            cluster_dir = folder / "Cluster"
            first = cluster_dir / "branch_elm_01.spm"
            second = cluster_dir / "branch_elm_02.spm"
            target = folder / "SK_Tree_elm_01.spm"
            source = folder / "Tree_elm_01.spm"
            tree_materials = [
                ("1", "branch_elm_01",
                 ["Cluster/branch_elm_01.tga"], ("1",)),
                ("2", "branch_elm_02",
                 ["Cluster/branch_elm_02.tga"], ("2",)),
            ]
            write_spm(target, tree_materials, mesh_ids=("1", "2"))
            write_spm(source, tree_materials, mesh_ids=("1", "2"))
            write_spm(first, [("1", "Source_01", [], ())])
            write_spm(second, [("1", "Source_02", [], ())])
            for cluster in (first, second):
                (cluster_dir / f"{cluster.stem}.tga").write_bytes(b"texture")
            write_ascii_fbx(
                folder / "fbx" / "Tree_elm_01.fbx",
                material_names=["branch_elm_01_Mat"],
                mesh_names=["branch_elm_01_mesh"],
                pairs=[("branch_elm_01_Mat", "branch_elm_01_mesh")],
            )
            usage = {
                str(first).casefold(): {
                    "spms": [str(target)],
                    "material_names": ["branch_elm_01"],
                    "material_names_by_spm": {
                        str(source): ["branch_elm_01"],
                    },
                    "source_refs": [
                        str(cluster_dir / "branch_elm_01.tga")
                    ],
                },
                str(second).casefold(): {
                    "spms": [str(target)],
                    "material_names": ["branch_elm_02"],
                    "material_names_by_spm": {
                        str(source): ["branch_elm_02"],
                    },
                    "source_refs": [
                        str(cluster_dir / "branch_elm_02.tga")
                    ],
                },
            }
            normalized = {
                "status": "ready",
                "variants": [{"ordinal": 1}],
                "production_normalization": {
                    "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                    "physical_capture_contract": {
                        "capture_maps": [{
                            "role": "Color",
                            "path": str(
                                cluster_dir / "branch_elm_01.tga"
                            ),
                        }],
                    },
                },
            }

            with mock.patch(
                "pcg_cluster_assembly_contract._atlas_normalized_variants",
                return_value=normalized,
            ) as lookup, mock.patch(
                "pcg_cluster_assembly_contract."
                "_validate_normalized_source_dependency",
            ):
                contract = build_cluster_assembly_contract(
                    folder,
                    [target],
                    [first, second],
                    cluster_usage=usage,
                    assembly_source_spms=[source],
                )

            dependencies = {
                row["name"]: row for row in contract["dependencies"]
            }
            lookup.assert_called_once()
            self.assertIs(
                dependencies["SK_branch_elm_01"]["normalized_variants"],
                normalized,
            )
            self.assertTrue(
                dependencies["SK_branch_elm_01"]["primary_role_source"]
            )
            self.assertEqual(
                dependencies["SK_branch_elm_02"]["decision"],
                "reference_only",
            )
            self.assertFalse(
                dependencies["SK_branch_elm_02"]["primary_role_source"]
            )
            self.assertIsNone(
                dependencies["SK_branch_elm_02"]["normalized_variants"]
            )
            self.assertEqual(
                dependencies["SK_branch_elm_01"]["texture_contract_source"],
                "atlas_physical_capture",
            )
            self.assertEqual(
                dependencies["SK_branch_elm_01"][
                    "tga_basename_validation"
                ]["status"],
                "ok",
            )
            self.assertEqual(
                dependencies["SK_branch_elm_02"][
                    "tga_basename_validation"
                ]["status"],
                "not_applicable",
            )

    def test_owner_folder_bark_identity_is_canonical_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "tree_NothofagusSolandri"
            cluster_dir = folder / "cluster"
            target = folder / "SK_tree_NothofagusSolandri_01.spm"
            provider = (
                cluster_dir
                / "SK_branch_tree_NothofagusSolandri_01.spm"
            )
            bark_name = "M_Bark_tree_NothofagusSolandri_01"
            bark_refs = [
                "texture/T_Bark_tree_NothofagusSolandri_01_color.tga",
                "texture/T_Bark_tree_NothofagusSolandri_01_normal.tga",
            ]
            write_spm(
                target,
                [("1", bark_name, bark_refs, ("1",))],
                mesh_ids=("1",),
            )
            write_spm(
                provider,
                [("1", bark_name, bark_refs, ("1",))],
                mesh_ids=("1",),
            )

            contract = _canonical_bark_contract(
                audit_module,
                folder,
                [target],
                [{
                    "source_spm": str(provider),
                    "spm": str(provider),
                }],
            )

            self.assertEqual(contract["status"], "canonical")
            self.assertEqual(contract["canonical_material"], bark_name)
            self.assertEqual(len(contract["canonical_sources"]), 1)


if __name__ == "__main__":
    unittest.main()
