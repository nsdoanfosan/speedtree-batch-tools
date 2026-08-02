import gzip
import hashlib
import json
import os
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]


def write_material_spm(path, materials, mesh_ids=()):
    material_xml = []
    for material_id, name, refs, owned_mesh_ids in materials:
        texture_xml = "".join(
            f"<TexFilename>{value}</TexFilename>" for value in refs
        )
        cutout_xml = "".join(
            f'<CutoutMesh ID="{value}"/>' for value in owned_mesh_ids
        )
        material_xml.append(
            f'<Material_v8 ID="{material_id}" Name="{name}">'
            f"{texture_xml}"
            f'<SupplementalCutoutMeshIDs Count="{len(owned_mesh_ids)}">'
            f"{cutout_xml}</SupplementalCutoutMeshIDs>"
            "</Material_v8>"
        )
    mesh_xml = "".join(
        f'<Mesh ID="{value}" Name="mesh-{value}"/>' for value in mesh_ids
    )
    payload = (
        "<SpeedTree><Materials>" + "".join(material_xml)
        + "</Materials><Meshes>" + mesh_xml
        + "</Meshes></SpeedTree>"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as handle:
        handle.write(payload)
    return path


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_convenience_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeRoot:
    def __init__(self):
        self.clipboard = ""

    def clipboard_clear(self):
        self.clipboard = ""

    def clipboard_append(self, value):
        self.clipboard += value

    @staticmethod
    def update_idletasks():
        return None


class FakeVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class FakeTree:
    def __init__(self):
        self.labels = {}
        self.selected = ()
        self.focused = None
        self.has_keyboard_focus = False

    @staticmethod
    def identify_region(_x, _y):
        return "tree"

    @staticmethod
    def identify_row(y):
        return y

    def item(self, iid, **values):
        if "text" in values:
            self.labels[iid] = values["text"]

    def selection_set(self, iid):
        self.selected = (iid,)

    def selection(self):
        return self.selected

    def focus(self, iid):
        self.focused = iid

    def focus_set(self):
        self.has_keyboard_focus = True


class SkBatchUiConvenienceTests(unittest.TestCase):
    def test_temporary_push_selection_requires_cluster_without_assembly(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ordinary = root / "tree_oak" / "SK_tree_oak_01.spm"
            pass_through = (
                root / "tree_pine" / "SK_tree_pine_01.spm"
            )
            missing = root / "tree_fir" / "SK_tree_fir_01.spm"
            assembly = root / "tree_elm" / "SK_tree_elm_01.spm"
            provider = (
                root / "tree_elm" / "cluster" /
                "SK_cluster_elm_01.spm"
            )
            pass_provider = (
                root / "tree_pine" / "Cluster" /
                "SK_cluster_pine_01.spm"
            )
            missing_provider = (
                root / "tree_fir" / "cluster" /
                "SK_cluster_fir_01.spm"
            )
            for spm in (
                ordinary,
                pass_through,
                missing,
                assembly,
                provider,
                pass_provider,
                missing_provider,
            ):
                spm.parent.mkdir(parents=True, exist_ok=True)
                spm.write_bytes(b"spm")

            pass_report = gui.repair_pipeline_report_path(pass_through)
            pass_report.parent.mkdir(parents=True, exist_ok=True)
            pass_report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {
                        "status": "pass_through",
                    },
                }),
                encoding="utf-8",
            )
            assembly_report = gui.repair_pipeline_report_path(assembly)
            assembly_report.parent.mkdir(parents=True, exist_ok=True)
            assembly_report.write_text(
                json.dumps({
                    "cluster_assembly_manifest": {
                        "status": "ready",
                        "manifest": {"path": "assembly.json"},
                    },
                }),
                encoding="utf-8",
            )

            self.assertFalse(
                gui.is_cluster_without_assembly_push_row(ordinary)
            )
            self.assertTrue(
                gui.is_cluster_without_assembly_push_row(pass_through)
            )
            self.assertTrue(
                gui.is_cluster_without_assembly_push_row(missing)
            )
            self.assertFalse(
                gui.is_cluster_without_assembly_push_row(assembly)
            )
            self.assertFalse(
                gui.is_cluster_without_assembly_push_row(provider)
            )
            self.assertFalse(
                gui.is_cluster_without_assembly_push_row(pass_provider)
            )

            app = gui.App.__new__(gui.App)
            app.tree = FakeTree()
            app.log = mock.Mock()
            app.items = {
                str(ordinary): {
                    "spm": ordinary,
                    "display_name": ordinary.name,
                    "checked": False,
                    "manual_bones_locked": False,
                },
                str(missing): {
                    "spm": missing,
                    "display_name": missing.name,
                    "checked": False,
                    "manual_bones_locked": False,
                },
                str(assembly): {
                    "spm": assembly,
                    "display_name": assembly.name,
                    "checked": True,
                    "manual_bones_locked": False,
                },
                str(provider): {
                    "spm": provider,
                    "display_name": provider.name,
                    "checked": True,
                    "manual_bones_locked": False,
                },
            }
            app.checked_rows = gui.CheckedRowController(
                app.items, app._redraw_checked_row
            )

            count = app._set_temporary_cluster_without_assembly_push_rows()

            self.assertEqual(count, 1)
            self.assertFalse(app.items[str(ordinary)]["checked"])
            self.assertTrue(app.items[str(missing)]["checked"])
            self.assertFalse(app.items[str(assembly)]["checked"])
            self.assertFalse(app.items[str(provider)]["checked"])
            self.assertTrue(
                app.tree.labels[str(ordinary)].startswith(gui.CHECK_OFF)
            )
            self.assertTrue(
                app.tree.labels[str(missing)].startswith(gui.CHECK_ON)
            )
            self.assertTrue(
                app.tree.labels[str(assembly)].startswith(gui.CHECK_OFF)
            )
            self.assertTrue(
                app.tree.labels[str(provider)].startswith(gui.CHECK_OFF)
            )

    def test_blender_repair_adds_and_blocks_only_exact_cluster_dependencies(self):
        gui = load_gui_module()
        owner = Path(r"D:\Trees\Birch")
        root_01 = owner / "SK_tree_birch_01.spm"
        root_02 = owner / "SK_tree_birch_02.spm"
        cluster_a = owner / "Cluster" / "SK_branch_birch_01.spm"
        cluster_b = owner / "Cluster" / "SK_leaf_birch_02.spm"
        root_items = [
            {"spm": root_01},
            {"spm": root_02},
        ]
        all_items = root_items + [
            {
                "spm": cluster_a,
                "referenced_by_spms": (root_01,),
            },
            {
                "spm": cluster_b,
                "referenced_by_spms": (),
            },
        ]

        with mock.patch.object(
            gui,
            "cluster_relation_output_targets",
            side_effect=lambda spm, _refs: (
                [root_01] if spm == cluster_a else []
            ),
        ):
            ordered, dependencies, auto_added = (
                gui.expand_blender_repair_targets(root_items, all_items)
            )

        self.assertEqual(
            [item["spm"] for item in ordered],
            [cluster_a, root_01, root_02],
        )
        self.assertEqual(dependencies[str(root_01)], (str(cluster_a),))
        self.assertEqual(dependencies[str(root_02)], ())
        self.assertEqual(auto_added, {str(cluster_a)})

    def test_blender_repair_maps_raw_tree_reference_to_selected_sk_output(self):
        gui = load_gui_module()
        owner = Path(r"D:\Trees\Elm")
        raw_tree = owner / "Tree_elm_02.spm"
        sk_tree = owner / "SK_Tree_elm_02.spm"
        cluster = owner / "Cluster" / "SK_branch_elm_01.spm"
        root_item = {
            "spm": sk_tree,
            "authoring_spm": sk_tree,
        }
        cluster_item = {
            "spm": cluster,
            "referenced_by_spms": (raw_tree,),
        }

        with mock.patch.object(
            gui,
            "cluster_relation_output_targets",
            return_value=[raw_tree],
        ):
            ordered, dependencies, auto_added = (
                gui.expand_blender_repair_targets(
                    [root_item],
                    [root_item, cluster_item],
                )
            )

        self.assertEqual(
            [item["spm"] for item in ordered],
            [cluster, sk_tree],
        )
        self.assertEqual(
            dependencies[str(sk_tree)],
            (str(cluster),),
        )
        self.assertEqual(auto_added, {str(cluster)})

    def test_blender_dependencies_intersect_registry_with_live_spm_usage(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "Tree_densiflora"
            cluster_dir = owner / "Cluster"
            cluster_dir.mkdir(parents=True)
            roots = [
                owner / f"SK_tree_densiflora_{index:02d}.spm"
                for index in range(1, 7)
            ]
            clusters = [
                cluster_dir / f"SK_cluster_densiflora_{index:02d}.spm"
                for index in range(1, 4)
            ]
            for path in roots + clusters:
                path.touch()
            from atlas_target_registry import save_target_registry

            save_target_registry(clusters[0].with_suffix(".blend"), roots)
            save_target_registry(clusters[1].with_suffix(".blend"), roots)
            save_target_registry(
                clusters[2].with_suffix(".blend"),
                roots[4:],
            )
            items = [{"spm": root} for root in roots]
            items.extend([
                {
                    "spm": clusters[0],
                    "referenced_by_spms": tuple(roots[:4]),
                },
                {
                    "spm": clusters[1],
                    "referenced_by_spms": tuple(roots[:4]),
                },
                {
                    "spm": clusters[2],
                    "referenced_by_spms": tuple(roots[4:]),
                },
            ])

            ordered, dependencies, auto_added = (
                gui.expand_blender_repair_targets(
                    [{"spm": roots[4]}],
                    items,
                )
            )

        self.assertEqual(
            [item["spm"] for item in ordered],
            [clusters[2], roots[4]],
        )
        self.assertEqual(
            dependencies[str(roots[4])],
            (str(clusters[2]),),
        )
        self.assertEqual(auto_added, {str(clusters[2])})

    def test_recent_24_hours_checks_only_recent_authoritative_spms(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recent = root / "SK_tree_recent.spm"
            old = root / "SK_tree_old.spm"
            missing = root / "SK_tree_missing.spm"
            mirror = root / "cluster_tree_old.spm"
            for path in (recent, old, mirror):
                path.write_bytes(path.name.encode("utf-8"))

            now_ns = time.time_ns()
            hour_ns = 60 * 60 * 1_000_000_000
            os.utime(recent, ns=(now_ns - 23 * hour_ns,) * 2)
            os.utime(old, ns=(now_ns - 25 * hour_ns,) * 2)
            # A recent mirror must not select an old authoritative Cluster SPM.
            os.utime(mirror, ns=(now_ns - hour_ns,) * 2)

            app = gui.App.__new__(gui.App)
            app.root = FakeRoot()
            app.tree = FakeTree()
            app.worker = None
            app.progress_var = FakeVar()
            messages = []
            app.log = messages.append
            app.items = {
                str(recent): {
                    "spm": recent,
                    "authoring_spm": recent,
                    "checked": False,
                    "manual_bones_locked": False,
                },
                str(old): {
                    "spm": old,
                    "authoring_spm": old,
                    "output_spm": mirror,
                    "checked": True,
                    "manual_bones_locked": False,
                },
                str(missing): {
                    "spm": missing,
                    "authoring_spm": missing,
                    "checked": True,
                    "manual_bones_locked": False,
                },
            }
            app.checked_rows = gui.CheckedRowController(
                app.items, app._redraw_checked_row
            )
            app.checked_rows.sync_after_reload()

            app._set_recent_modified(hours=24, now_ns=now_ns)

            self.assertTrue(app.items[str(recent)]["checked"])
            self.assertFalse(app.items[str(old)]["checked"])
            self.assertFalse(app.items[str(missing)]["checked"])
            self.assertFalse(app.checked_rows.armed)
            self.assertIn("1개 체크", app.progress_var.value)
            self.assertIn("작업은 실행하지 않음", messages[-1])

    def test_cluster_raw_inputs_are_retained_as_canonical_bootstrap_rows(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cluster = root / "bush_Silky_Dogwood" / "Cluster"
            cluster.mkdir(parents=True)
            source = cluster / "cluster_Silky_Dogwood_01.spm"
            source.write_bytes(b"source")
            unused = cluster / "cluster_Silky_Dogwood_02.spm"
            unused.write_bytes(b"unused")
            speedtree_temp = cluster / "~cluster_Silky_Dogwood_01.spm"
            speedtree_temp.write_bytes(b"temporary")
            full_sk = root / "bush_Silky_Dogwood" / "SK_bush_Silky_Dogwood_01.spm"
            write_material_spm(
                full_sk,
                [(
                    "material-1",
                    "M_cluster_Silky_Dogwood_01",
                    ["Cluster/cluster_Silky_Dogwood_01.tga"],
                    ("mesh-1",),
                )],
                mesh_ids=("mesh-1",),
            )

            rows = gui.scan_cluster_spm_sources(root)

            self.assertEqual(len(rows), 1)
            row = next(
                item for item in rows
                if item["legacy_output_spm"] == source
            )
            self.assertEqual(
                row["source_spm"], cluster / "SK_cluster_Silky_Dogwood_01.spm"
            )
            self.assertEqual(row["authoring_spm"], row["source_spm"])
            self.assertEqual(row["output_spm"], row["source_spm"])
            self.assertEqual(row["pair_status"], "normalization_ready")
            self.assertIn("connected_output_textures", row)
            self.assertNotIn(unused, [item["output_spm"] for item in rows])
            self.assertNotIn(
                speedtree_temp,
                [row["source_spm"] for row in rows],
            )
            self.assertEqual(
                row["blend_path"],
                cluster / "SK_cluster_Silky_Dogwood_01.blend",
            )
            self.assertEqual(
                gui.scan_sk_spms(root),
                [full_sk],
            )
            self.assertEqual(
                gui.blend_path_for(source),
                cluster / "SK_cluster_Silky_Dogwood_01.blend",
            )
            self.assertEqual(
                gui.blend_path_for(full_sk),
                full_sk.with_suffix(".blend"),
            )

    def test_cluster_scan_uses_authoritative_source_and_prunes_pipeline_backup(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / "Tree_elm"
            cluster = owner / "Cluster"
            branch = cluster / "branch_elm_01.spm"
            branch.parent.mkdir(parents=True)
            branch.write_bytes(b"cluster-source")
            write_material_spm(
                owner / "SK_Tree_elm_01.spm",
                [(
                    "material-1",
                    "M_branch_elm_01",
                    ["texture/T_branch_elm_01_color.tga"],
                    ("mesh-1",),
                )],
                mesh_ids=("mesh-1",),
            )
            write_material_spm(
                owner / "Tree_elm_01.spm",
                [(
                    "material-1",
                    "branch_elm_01",
                    ["Cluster/branch_elm_01.tga"],
                    ("mesh-1",),
                )],
                mesh_ids=("mesh-1",),
            )

            backup_owner = (
                root / "_atlas_cluster_normalization_backups" / "final"
                / "files" / "Tree_elm"
            )
            backup_cluster = backup_owner / "Cluster"
            backup_branch = backup_cluster / "branch_elm_01.spm"
            backup_branch.parent.mkdir(parents=True)
            backup_branch.write_bytes(b"backup-cluster-source")
            write_material_spm(
                backup_owner / "Tree_elm_01.spm",
                [(
                    "material-1",
                    "branch_elm_01",
                    ["Cluster/branch_elm_01.tga"],
                    ("mesh-1",),
                )],
                mesh_ids=("mesh-1",),
            )

            rows = gui.scan_cluster_spm_sources(root)

            self.assertEqual(
                [row["source_spm"] for row in rows],
                [cluster / "SK_branch_elm_01.spm"],
            )
            self.assertNotIn(
                backup_branch,
                [row["source_spm"] for row in rows],
            )

    def test_cluster_folder_chain_and_owner_wind_cover_tree_bush_and_weed(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                ("Tree_elm", "branch_elm_01.spm", "TREE"),
                ("bush_Silky_Dogwood", "cluster_Dogwood_01.spm", "BUSH"),
                ("weed_ladyfern", "cluster_ladyfern_01.spm", "GRASS"),
            )
            for owner, name, expected_wind in cases:
                spm = root / owner / "Cluster" / name
                self.assertEqual(gui.wind_preset_for_spm(spm), expected_wind)
                self.assertEqual(
                    gui.sk_batch_folder_chain(root, spm),
                    [root / owner, root / owner / "Cluster"],
                )

    def test_cluster_row_shows_only_canonical_output_name(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        iid = "cluster-source"
        app.items = {
            iid: {
                "spm": Path("Tree_elm/Cluster/SK_branch_elm_01.spm"),
                "cluster_source_spm": Path(
                    "Tree_elm/Cluster/branch_elm_01.spm"
                ),
                "output_spm": Path("Tree_elm/Cluster/SK_branch_elm_01.spm"),
                "legacy_output_spm": Path(
                    "Tree_elm/Cluster/branch_elm_01.spm"
                ),
                "display_name": "SK_branch_elm_01.spm",
                "source_read_only": False,
                "checked": True,
                "manual_bones_locked": False,
            }
        }

        label = app._item_label(iid)

        self.assertIn("SK_branch_elm_01.spm", label)
        self.assertNotIn("branch_elm_01.spm →", label)
        self.assertNotIn("Atlas output", label)
        # Cluster rows use the dedicated one-root-per-piece stage-① policy.
        self.assertTrue(gui.should_calibrate_spm(app.items[iid]))

    def test_only_cluster_authoring_spm_uses_root_only_calibration(self):
        # Canonical and legacy Cluster row names both route through stage ①;
        # spm_audit selects the first renderable structural roots and writes
        # Absolute/1 instead of using the normal tree density solver.
        gui = load_gui_module()
        for name in ("SK_branch_elm_01.spm", "branch_elm_01.spm"):
            item = {
                "spm": Path("Tree_elm/Cluster") / name,
                "source_read_only": False,
                "manual_bones_locked": False,
            }
            self.assertTrue(gui.should_calibrate_spm(item), name)

        read_only_item = {
            "spm": Path("Tree_elm/Cluster/SK_branch_elm_01.spm"),
            "source_read_only": True,
            "manual_bones_locked": False,
        }
        self.assertFalse(gui.should_calibrate_spm(read_only_item))

        tree_item = {
            "spm": Path("Tree_elm/SK_Tree_elm_01.spm"),
            "source_read_only": False,
            "manual_bones_locked": False,
        }
        self.assertFalse(gui.should_calibrate_spm(tree_item))

    def test_canonical_atlas_manifest_preflight_runs_before_blender(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.log = mock.Mock()
        spm = Path("Tree_elm/Cluster/SK_branch_elm_01.spm")
        result = {
            "status": "updated",
            "updated": ["scope.json"],
            "current": [],
            "pending": [],
        }

        with mock.patch.object(
            gui,
            "refresh_atlas_manifests_for_spm",
            return_value=result,
        ) as refresh:
            actual = app._refresh_canonical_atlas_manifests(spm)

        self.assertEqual(actual, result)
        refresh.assert_called_once_with(spm, require_complete=True)
        app.log.assert_called_once()

    def test_canonical_atlas_manifest_preflight_skips_owner_spm(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.log = mock.Mock()
        spm = Path("Tree_elm/SK_tree_elm_01.spm")

        with mock.patch.object(
            gui,
            "refresh_atlas_manifests_for_spm",
        ) as refresh:
            actual = app._refresh_canonical_atlas_manifests(spm)

        self.assertEqual(actual["status"], "not_applicable")
        self.assertEqual(
            actual["reason"],
            "owner_spm_is_not_an_atlas_producer",
        )
        refresh.assert_not_called()
        app.log.assert_not_called()

    def test_canonical_atlas_conflict_preserves_repair_evidence(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.log = mock.Mock()
        spm = Path("Tree_elm/Cluster/SK_branch_elm_01.spm")
        report = {
            "reason_token": "atlas_manifest_mirror_conflict_repairable",
            "evidence": {
                "status": "repairable",
                "authority": "authority.json",
                "mirrors": ["stale.json"],
            },
        }
        failure = gui.CanonicalOutputManifestError(
            "ownership conflict",
            report=report,
        )

        with mock.patch.object(
            gui,
            "refresh_atlas_manifests_for_spm",
            side_effect=failure,
        ):
            with self.assertRaises(gui.BatchItemError) as caught:
                app._refresh_canonical_atlas_manifests(spm)

        self.assertEqual(
            caught.exception.report["reason_token"],
            "atlas_manifest_mirror_conflict_repairable",
        )
        self.assertEqual(
            caught.exception.report["evidence"]["status"], "repairable"
        )
        self.assertIn("자동 복구 대상", str(caught.exception))
        self.assertIn("원인:", str(caught.exception))
        self.assertIn("조치:", str(caught.exception))
        self.assertNotIn("preflight failed", str(caught.exception))

    def test_canonical_atlas_real_ownership_conflict_is_korean_final_block(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.log = mock.Mock()
        spm = Path("Tree_elm/Cluster/SK_branch_elm_01.spm")
        failure = gui.CanonicalOutputManifestError(
            "ownership conflict",
            report={
                "reason_token": "atlas_manifest_ownership_conflict",
                "evidence": {
                    "status": "unrepairable",
                    "reason": "different source identity",
                },
            },
        )

        with mock.patch.object(
            gui,
            "refresh_atlas_manifests_for_spm",
            side_effect=failure,
        ):
            with self.assertRaises(gui.BatchItemError) as caught:
                app._refresh_canonical_atlas_manifests(spm)

        message = str(caught.exception)
        self.assertIn("최종 차단", message)
        self.assertIn("서로 다른 원본", message)
        self.assertIn("임의로 덮어쓰지 않습니다", message)
        self.assertNotIn("ownership conflict", message)

    def test_cluster_job_normalizes_once_and_never_republishes_legacy_name(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary) / "Tree_elm" / "Cluster"
            cluster.mkdir(parents=True)
            raw = cluster / "branch_elm_01.spm"
            canonical = cluster / "SK_branch_elm_01.spm"
            raw.write_bytes(b"generation-1")

            bootstrap = gui.prepare_cluster_spm_pair_for_job(raw)
            self.assertEqual(
                bootstrap["operation"],
                "normalize_legacy_output_to_canonical",
            )
            self.assertEqual(canonical.read_bytes(), b"generation-1")

            canonical.write_bytes(b"generation-2")
            current = gui.prepare_cluster_spm_pair_for_job(canonical)
            self.assertEqual(current["operation"], "none")
            self.assertEqual(raw.read_bytes(), b"generation-1")

            raw.write_bytes(b"independent-atlas-edit")
            gui.prepare_cluster_spm_pair_for_job(canonical)
            self.assertEqual(canonical.read_bytes(), b"generation-2")

    def test_folder_click_clears_stale_actionable_checks(self):
        gui = load_gui_module()
        app = gui.App.__new__(gui.App)
        app.root = FakeRoot()
        app.tree = FakeTree()
        app.worker = None
        app.cell_editor = None
        spm = Path("Tree_elm/SK_Tree_elm_01.spm")
        app.items = {
            str(spm): {
                "spm": spm,
                "checked": True,
                "manual_bones_locked": False,
            }
        }
        folder_iid = "folder::cluster"
        app.folder_rows = {folder_iid: Path("Tree_elm/Cluster")}
        app.checked_rows = gui.CheckedRowController(
            app.items, app._redraw_checked_row
        )
        app.checked_rows.sync_after_reload()

        event = type("Event", (), {"x": 0, "y": folder_iid})()
        self.assertEqual(app._on_click(event), "break")

        self.assertFalse(app.items[str(spm)]["checked"])
        self.assertEqual(app.tree.selection(), (folder_iid,))
        self.assertEqual(app.tree.focused, folder_iid)

    def test_verified_xml_bone_count_is_read_only_and_content_matched(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_manual_01.spm"
            spm.write_bytes(b"current-spm")
            xml_path = root / "xml" / f"{spm.stem}.xml"
            xml_path.parent.mkdir()
            xml_path.write_text(
                (
                    f'<SpeedTreeRaw Source="{spm}"><Bones Count="2">'
                    '<Bone ID="0" Generator="Branch" />'
                    '<Bone ID="1" Generator="Branch 2" />'
                    "</Bones></SpeedTreeRaw>"
                ),
                encoding="utf-8",
            )
            receipt_path = (
                xml_path.parent
                / ".speedtree_export_cache"
                / f"{xml_path.name}.json"
            )
            receipt_path.parent.mkdir()
            receipt_path.write_text(
                json.dumps(
                    {
                        "inputs": {
                            "spm": {
                                "path": str(spm),
                                "size": spm.stat().st_size,
                                "sha256": hashlib.sha256(spm.read_bytes()).hexdigest(),
                            }
                        },
                        "artifacts": [
                            {
                                "relative_path": xml_path.name,
                                "size": xml_path.stat().st_size,
                                "sha256": hashlib.sha256(
                                    xml_path.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            before = (
                spm.read_bytes(),
                spm.stat().st_mtime_ns,
                xml_path.read_bytes(),
                xml_path.stat().st_mtime_ns,
            )

            measurement = gui.current_speedtree_bone_measurement(spm)

            self.assertEqual(measurement["count"], 2)
            self.assertTrue(measurement["current"])
            self.assertEqual(
                before,
                (
                    spm.read_bytes(),
                    spm.stat().st_mtime_ns,
                    xml_path.read_bytes(),
                    xml_path.stat().st_mtime_ns,
                ),
            )
            spm.write_bytes(b"changed-spm")
            stale = gui.current_speedtree_bone_measurement(spm)
            self.assertEqual(stale["count"], 2)
            self.assertFalse(stale["current"])

    def test_compact_table_keeps_full_selected_row_detail(self):
        gui = load_gui_module()
        full = "수동 본 유지 🔒 · SpeedTree 본 282개 (현재 SPM과 일치하는 XML)"

        compact = gui.compact_table_status(full, max_chars=24)
        detail = gui.selected_row_detail_text(
            Path("SK_tree_birch_paper_03.spm"),
            {
                "spm_status": full,
                "blend_status": "최신 ✓",
                "push_status": "준비됨 ✓",
            },
        )

        self.assertLessEqual(len(compact), 24)
        self.assertTrue(compact.endswith("…"))
        self.assertIn(full, detail)
        self.assertIn("SK_tree_birch_paper_03.spm", detail)

    def test_spm_check_uses_clear_bone_style_names(self):
        gui = load_gui_module()
        parts = gui.spm_check_status_parts(
            {
                "generators": [
                    {"style": 0.0, "bones": 2.0},
                    {"style": 1.0, "bones": 0.5},
                    {"style": 0.0, "bones": 0.0},
                ],
                "materials": [{"needs_prefix": True}],
                "bone_graph": {
                    "root_target_generator_count": 2,
                    "base_excluded_generator_count": 1,
                    "unknown_base_generators": [{"name": "Unknown"}],
                },
            }
        )

        self.assertEqual(
            parts,
            [
                "고정 본(Absolute) 1개",
                "자동 본(Relative) 1개",
                "본 꺼짐 1개",
                "M_ 필요 1개",
                "자동 대상 2 / Base 제외 1",
                "Base 미분류 1",
            ],
        )
        self.assertNotIn("미보정", " · ".join(parts))

    def test_first_click_isolates_row_and_ctrl_c_copies_that_spm_path(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            spms = [Path(temporary) / f"SK_tree_{index}.spm" for index in range(3)]
            app = gui.App.__new__(gui.App)
            app.root = FakeRoot()
            app.tree = FakeTree()
            app.worker = None
            app.cell_editor = None
            app.progress_var = FakeVar()
            app.items = {
                str(spm): {
                    "spm": spm,
                    "checked": True,
                    "manual_bones_locked": False,
                }
                for spm in spms
            }
            app.checked_rows = gui.CheckedRowController(
                app.items, app._redraw_checked_row
            )
            app.checked_rows.sync_after_reload()

            clicked = str(spms[1])
            event = type("Event", (), {"x": 0, "y": clicked})()
            self.assertEqual(app._on_click(event), "break")

            self.assertEqual(
                [entry["checked"] for entry in app.items.values()],
                [False, True, False],
            )
            self.assertEqual(app.tree.selection(), (clicked,))
            self.assertEqual(app.tree.focused, clicked)
            self.assertTrue(app.tree.has_keyboard_focus)

            self.assertEqual(app.copy_selected_paths(), "break")
            self.assertEqual(app.root.clipboard, str(spms[1].resolve()))
            self.assertIn("1개", app.progress_var.value)


    def test_running_queue_allows_selecting_the_next_execution_target(self):
        gui = load_gui_module()

        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "SK_tree_running_a.spm"
            second = Path(temporary) / "SK_tree_running_b.spm"
            app = gui.App.__new__(gui.App)
            app.root = FakeRoot()
            app.tree = FakeTree()
            app.worker = object()
            app.active_batch_job = {"id": 1}
            app.cell_editor = None
            app.progress_var = FakeVar()
            app.items = {
                str(path): {
                    "spm": path,
                    "checked": path == first,
                    "manual_bones_locked": False,
                }
                for path in (first, second)
            }
            app.row_copy_paths = {
                str(first): [first],
                str(second): [second],
            }
            app.checked_rows = gui.CheckedRowController(
                app.items, app._redraw_checked_row
            )
            app.checked_rows.sync_after_reload()
            clear_current = type(
                "Event", (), {"x": 0, "y": str(first)}
            )()
            select_next = type(
                "Event", (), {"x": 0, "y": str(second)}
            )()

            self.assertEqual(app._on_click(clear_current), "break")
            self.assertEqual(app._on_click(select_next), "break")

            self.assertFalse(app.items[str(first)]["checked"])
            self.assertTrue(app.items[str(second)]["checked"])
            self.assertEqual(app.tree.selection(), (str(second),))
            self.assertEqual(app.copy_selected_paths(), "break")
            self.assertEqual(app.root.clipboard, str(second.resolve()))


if __name__ == "__main__":
    unittest.main()
