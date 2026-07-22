import gzip
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from pcg_cluster_assembly_contract import (
    ClusterAssemblyReceiptStaleError,
    build_cluster_assembly_contract,
    classify_fbx_role,
    load_cluster_assembly_receipt,
    locate_cluster_assembly_receipt,
    persist_cluster_assembly_receipt,
    persist_cluster_assembly_receipts,
    inspect_fbx_material_mesh_pairs,
)
from pcg_texture_audit import (
    cluster_material_usage,
    cluster_source_inventory,
    cluster_spms,
    current_leaf_atlas_inventory,
    extract_material_image_refs,
    m_prefix_plan,
)


REAL_ELM_SOURCE_FBX = Path(
    r"D:\OneDrive\Forestportfolio\02_nature\Tree\Tree_elm"
    r"\fbx\tree_elm_01.fbx"
)
REAL_ELM_CLUSTER_LEAF = Path(
    r"D:\OneDrive\Forestportfolio\02_nature\Tree\Tree_elm"
    r"\Cluster\leaf_elm_01.spm"
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


def write_spm(path, materials, mesh_ids=()):
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
    payload = (
        "<SpeedTree><Materials>" + "".join(material_xml)
        + "</Materials><Meshes>" + mesh_xml
        + "</Meshes></SpeedTree>"
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
        self.assertEqual(branch["complete_pair_count"], 214)
        self.assertEqual(leaf["decision"], "normalize_part")
        self.assertEqual(leaf["complete_pair_count"], 376)


class ClusterAssemblyContractTests(unittest.TestCase):
    def test_cluster_source_inventory_excludes_generated_sk_and_keeps_generic_names(self):
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
            self.assertEqual(sources, [branch, generic])
            usage = {
                str(generic).lower(): {
                    "spms": [str(folder / "SK_bush_Silky_Dogwood_01.spm")],
                    "material_names": ["cluster_Silky_Dogwood_01"],
                    "source_refs": [str(cluster / "cluster_Silky_Dogwood_01.tga")],
                },
            }
            rows = cluster_source_inventory(sources, usage, {"dependencies": []})

            self.assertEqual([row["name"] for row in rows], [
                "branch_Silky_Dogwood_01", "cluster_Silky_Dogwood_01",
            ])
            generic_row = rows[1]
            self.assertTrue(generic_row["referenced"])
            self.assertEqual(len(generic_row["cluster_output_textures"]), 1)
            self.assertIsNone(generic_row["assembly_role"])

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
                {"branch_elm_01", "leaf_elm_01", "leaf_elm_side_01"},
            )
            self.assertEqual(dependencies["branch_elm_01"]["decision"], "normalize_part")
            self.assertEqual(dependencies["leaf_elm_01"]["decision"], "blocked")
            self.assertEqual(dependencies["leaf_elm_side_01"]["decision"], "reference_only")
            self.assertEqual(
                [row["name"] for row in children["leaf"]["references"]],
                ["leaf_elm_side_01"],
            )
            self.assertEqual(contract["canonical_bark"]["status"], "replacement_required")
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
                ["branch_elm_01", "leaf_elm_01"],
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
                dependencies["branch_elm_01"]["tga_basename_validation"]["status"],
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
        targets = gui.selected_cluster_m_targets(
            ["bush-child"], {"bush-child": [source]}
        )
        self.assertEqual(targets, [Path(source)])

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

    def test_cluster_child_m_preview_never_creates_an_sk_spm(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            branch = cluster / "branch_elm_01.spm"
            write_spm(branch, [("mat-bark", "Bark_elm_01", [], [])])
            before = branch.read_bytes()

            preview = gui.prepare_cluster_m_prefix(branch, dry_run=True)

            self.assertEqual(preview["spm"], str(branch))
            self.assertEqual(preview["renames"], [["Bark_elm_01", "M_Bark_elm_01"]])
            self.assertEqual(
                Path(preview["blend_output"]),
                cluster / "SK_branch_elm_01.blend",
            )
            self.assertEqual(branch.read_bytes(), before)
            self.assertFalse((cluster / "SK_branch_elm_01.spm").exists())

    def test_cluster_child_m_apply_keeps_source_name_and_creates_backup(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "bush_Silky_Dogwood" / "Cluster"
            source = cluster / "cluster_Silky_Dogwood_01.spm"
            write_spm(source, [("mat-bark", "Bark_Dogwood_01", [], [])])

            result = gui.prepare_cluster_m_prefix(source, dry_run=False)

            self.assertEqual(result["spm"], str(source))
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertEqual(
                Path(result["blend_output"]),
                cluster / "SK_cluster_Silky_Dogwood_01.blend",
            )
            self.assertTrue(source.is_file())
            self.assertFalse(
                (cluster / "SK_cluster_Silky_Dogwood_01.spm").exists())
            self.assertEqual(
                m_prefix_plan(source),
                [],
            )

    def test_weed_cluster_m_apply_preserves_raw_stem_and_tga_references(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "weed_Clover" / "Cluster"
            source = cluster / "cluster_weed_Clover_01.spm"
            refs = [
                "cluster_weed_Clover_01.tga",
                "cluster_weed_Clover_01_Opacity.tga",
            ]
            write_spm(source, [("5", "leaf_weed_Clover_01", refs, [])])
            before_refs = extract_material_image_refs(source)[0]["refs"]

            preview = gui.prepare_cluster_m_prefix(source, dry_run=True)
            result = gui.prepare_cluster_m_prefix(source, dry_run=False)

            self.assertEqual(
                preview["renames"],
                [["leaf_weed_Clover_01", "M_leaf_weed_Clover_01"]],
            )
            self.assertEqual(result["spm"], str(source))
            self.assertEqual(
                Path(result["blend_output"]),
                cluster / "SK_cluster_weed_Clover_01.blend",
            )
            self.assertEqual(
                extract_material_image_refs(source)[0]["refs"],
                before_refs,
            )
            self.assertFalse(
                (cluster / "SK_cluster_weed_Clover_01.spm").exists())

    @unittest.skipUnless(
        REAL_ELM_CLUSTER_LEAF.is_file(),
        "Tree Elm Cluster leaf source unavailable",
    )
    def test_real_leaf_cluster_preview_covers_actual_export_materials(self):
        gui = load_gui_module()

        preview = gui.prepare_cluster_m_prefix(
            REAL_ELM_CLUSTER_LEAF, dry_run=True)
        renames = dict(preview["renames"])

        self.assertEqual(
            renames["Bark_tree_NothofagusSolandri_01"],
            "M_Bark_tree_NothofagusSolandri_01",
        )
        self.assertEqual(renames["leaf_01"], "M_leaf_01")

    def test_blender_helper_row_copies_only_the_vegetation_folder(self):
        gui = load_gui_module()
        folder = r"D:\OneDrive\Forestportfolio\02_nature\Weed\weed_Clover"
        item = {
            "folder": folder,
            "cluster_source_rows": [{
                "source": folder + r"\Cluster\cluster_weed_Clover_01.spm",
            }],
        }

        self.assertEqual(gui.blender_helper_copy_paths(item), [folder])

    def test_prepare_sk_rejects_cluster_folder_even_in_dry_run(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            source = cluster / "branch_elm_01.spm"
            write_spm(source, [("mat-bark", "Bark_elm_01", [], [])])

            with self.assertRaisesRegex(RuntimeError, "Cluster source SPM"):
                gui.prepare_sk(cluster, dry_run=True)

            self.assertTrue(source.is_file())
            self.assertFalse((cluster / "SK_branch_elm_01.spm").exists())

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
        app._update_step3_button = lambda: None
        event = type("Event", (), {"x": 0, "y": 0})()

        self.assertEqual(app._on_click(event), "break")
        self.assertEqual(app.tree.selected, ("cluster-child",))
        self.assertEqual(app.tree.focused, "cluster-child")
        self.assertTrue(app.checked_rows.cleared)


if __name__ == "__main__":
    unittest.main()
