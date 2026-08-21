import argparse
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
SK_DIR = REPO / "sk_batch"
JOBS_DIR = SK_DIR / "jobs"
for path in (REPO, SK_DIR, JOBS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import speedtree_material_preflight as preflight
from sk_batch.spm_leaf_handoff_contract import (
    inspect_spm_mesh_file_references,
)
from speedtree_texture_contract import REQUIRED_TEXTURE_ROLES


def write_spm(
    path,
    mesh_filenames=(),
    referenced_external_mesh_indexes=(),
    material_external_mesh_indexes=(),
    managed_leaf=False,
    leaf_hidden=False,
    leaf_default_cutout=False,
):
    model = ET.Element("SpeedTreeModel")
    assets = ET.SubElement(model, "Assets")
    leaf_material = None
    for material_id, name, mesh_id in (
        (1, "M_leaf_grass_dead", 11),
        (2, "M_stem_common_01", 12),
    ):
        material = ET.SubElement(
            assets, "Material_v8", ID=str(material_id), Name=name
        )
        ET.SubElement(material, "CutoutMeshID").text = str(mesh_id)
        ET.SubElement(assets, "Mesh", ID=str(mesh_id), Name=f"mesh_{mesh_id}")
        if material_id == 1:
            leaf_material = material
    if managed_leaf:
        ET.SubElement(leaf_material, "UserData").text = json.dumps(
            {
                "generator": "Atlas Leaf Mesh Builder",
                "scope": "test-scope",
                "kind": "material",
            }
        )
    external_mesh_ids = []
    for index, filename in enumerate(mesh_filenames, 90):
        mesh = ET.SubElement(
            assets, "Mesh", ID=str(index), Name=f"plate_{index}"
        )
        ET.SubElement(mesh, "Filename").text = filename
        ET.SubElement(mesh, "Embedded").text = "false"
        external_mesh_ids.append(index)
    supplemental_indexes = tuple(
        dict.fromkeys(
            tuple(referenced_external_mesh_indexes)
            + tuple(material_external_mesh_indexes)
        )
    )
    if supplemental_indexes:
        supplemental = ET.SubElement(
            leaf_material,
            "SupplementalCutoutMeshIDs",
            Count=str(len(supplemental_indexes)),
        )
        for external_index in supplemental_indexes:
            ET.SubElement(
                supplemental,
                "CutoutMesh",
                ID=str(external_mesh_ids[external_index]),
            )

    tree = ET.SubElement(model, "Generator", Type="Tree")
    properties = ET.SubElement(tree, "Properties")
    prop = ET.SubElement(properties, "Property")
    ET.SubElement(prop, "Name").text = "SpeedTree SDK:User data"
    ET.SubElement(prop, "Value").text = ""

    for generator_type, guid, property_name, material_id, mesh_id in (
        (
            "Leaf Mesh",
            "leaf-guid",
            "Leaves:Type:0",
            1,
            -10 if leaf_default_cutout else 11,
        ),
        ("Branch", "stem-guid", "Branches", 2, None),
    ):
        generator = ET.SubElement(model, "Generator", Type=generator_type)
        ET.SubElement(generator, "GUID").text = guid
        ET.SubElement(generator, "Name").text = guid
        ET.SubElement(generator, "Hidden").text = (
            "true" if generator_type == "Leaf Mesh" and leaf_hidden
            else "false"
        )
        properties = ET.SubElement(generator, "Properties")
        values = [("Material", material_id)]
        if mesh_id is not None:
            values.append(("Mesh", mesh_id))
        for suffix, value in values:
            prop = ET.SubElement(properties, "Property")
            ET.SubElement(prop, "Name").text = f"{property_name}:{suffix}"
            ET.SubElement(prop, "Value").text = str(value)
        if generator_type == "Leaf Mesh":
            for slot_index, external_index in enumerate(
                referenced_external_mesh_indexes,
                start=1,
            ):
                external_mesh_id = external_mesh_ids[external_index]
                for suffix, value in (
                    ("Material", 1),
                    ("Mesh", external_mesh_id),
                ):
                    prop = ET.SubElement(properties, "Property")
                    ET.SubElement(prop, "Name").text = (
                        f"Leaves:Type:{slot_index}:{suffix}"
                    )
                    ET.SubElement(prop, "Value").text = str(value)
        node = ET.SubElement(model, "Node", Type=generator_type)
        ET.SubElement(node, "GeneratorGUID").text = guid
        ET.SubElement(node, "ParentGUID").text = "parent"
        ET.SubElement(node, "Name").text = guid + "-node"
        ET.SubElement(node, "GUID").text = guid + "-node"
        ET.SubElement(node, "Hidden").text = "false"
        extra = ET.SubElement(node, "Extra")
        ET.SubElement(extra, "m_bDeleted").text = "false"
        ET.SubElement(extra, "m_bCulled").text = "false"

    path.write_bytes(gzip.compress(ET.tostring(model, encoding="utf-8")))


def write_stmat(spm, material_names):
    texture_dir = spm.parent / "texture"
    texture_dir.mkdir(parents=True, exist_ok=True)
    sources = {}
    for role in REQUIRED_TEXTURE_ROLES:
        source = texture_dir / f"T_grass_shared_{role}.tga"
        source.write_bytes(role.encode("ascii"))
        sources[role] = source
    root = ET.Element("SpeedTreeMaterials")
    for index, name in enumerate(material_names, 1):
        material = ET.SubElement(root, "Material", ID=str(index), Name=name)
        for role, source in sources.items():
            ET.SubElement(material, "Map", Name=role, Source=str(source))
    stmat = spm.parent / "fbx" / f"{spm.stem}.stmat"
    stmat.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(stmat, encoding="utf-8", xml_declaration=True)
    now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
    os.utime(spm, ns=(now, now))
    os.utime(stmat, ns=(now + 1, now + 1))
    return stmat


class SpeedTreeMaterialPreflightTests(unittest.TestCase):
    def test_managed_bindings_skip_unmanaged_provenance_audit(self):
        readiness = {
            "status": "ok",
            "bindings": [{
                "material": "M_Bark_test_Mat",
                "material_index": 0,
                "status": "ok",
                "resolved": {"color": "T_Bark_test_color.tga"},
            }],
            "missing": [],
            "warnings": [],
        }
        with mock.patch.object(
            preflight,
            "read_stmat_material_sources",
        ) as read_stmat, mock.patch.object(
            preflight,
            "inspect_spm_texture_slots",
        ) as inspect_slots, mock.patch.object(
            preflight,
            "resolve_atlas_manifests",
        ) as resolve_atlas, mock.patch.object(
            preflight,
            "atlas_provisional_source_declarations",
        ) as provisional, mock.patch.object(
            preflight,
            "_capture_live_material_export_evidence",
        ) as live_evidence:
            result = preflight.augment_texture_readiness_contract(
                readiness,
                "unused.stmat",
                "unused.spm",
            )

        self.assertEqual(result, readiness)
        read_stmat.assert_not_called()
        inspect_slots.assert_not_called()
        resolve_atlas.assert_not_called()
        provisional.assert_not_called()
        live_evidence.assert_not_called()

    def evaluate_issue64_fixture(self, mutate_snapshot=None):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "issue64_inactive_material_preflight.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "sanitized_cluster03"
            spm = asset / "SK_cluster_species_03.spm"
            stmat = asset / "fbx" / "SK_cluster_species_03.stmat"
            stmat.parent.mkdir(parents=True)
            spm.write_bytes(b"sanitized fixture; no production SPM content")
            foreign = stmat.parent / "foreign_species_leaf_color.tif"
            foreign.write_bytes(b"foreign provisional pixels")
            self._write_raw_stmat(
                stmat,
                fixture["material"]["material_name"],
                {"Color": foreign},
            )
            snapshot = json.loads(json.dumps(
                fixture["live_export_snapshot"]
            ))
            snapshot["spm"] = str(spm.resolve())
            if mutate_snapshot is not None:
                mutate_snapshot(snapshot)
            inspection = {
                "materials": [{
                    "material_id": fixture["material"]["material_id"],
                    "material_name": fixture["material"]["material_name"],
                    "slots": [],
                }],
            }
            with mock.patch.object(
                preflight,
                "inspect_spm_texture_slots",
                return_value=inspection,
            ), mock.patch.object(
                preflight,
                "live_generator_delivery_snapshot",
                return_value=snapshot,
            ):
                result = preflight.augment_texture_readiness_contract(
                    preflight.resolve_texture_bindings(stmat),
                    stmat,
                    spm,
                    source_texture_roots=[],
                    leaf_contract=fixture["leaf_reference_contract"],
                    all_material_contract=(
                        fixture["all_export_material_contract"]
                    ),
                    export_evidence_spm=spm,
                )
            issues = preflight.preflight_contract_issues({
                "texture_readiness_contract": result,
            })
        return fixture, result, issues

    def test_export_reports_shared_slot_boundary_and_restores_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_test.spm"
            options = root / "Options.ini"
            xml_options = root / "OptionsXml.ini"
            spm.write_bytes(b"spm")
            options.write_text("[Export]", encoding="utf-8")
            xml_options.write_text("[Export]", encoding="utf-8")
            args = argparse.Namespace(
                spm=str(spm),
                fbx_ini=str(options),
                xml_ini=str(xml_options),
                speedtree_exe=str(root / "SpeedTree.exe"),
                timeout=900,
                native_process_timeout=180,
            )
            helper = mock.Mock()
            events = []

            @contextmanager
            def original_gate():
                events.append("gate_enter")
                yield
                events.append("gate_exit")

            def export_bundle(**kwargs):
                with helper.speedtree_export_gate():
                    events.append("export")
                    events.append(os.environ.get(
                        "SPEEDTREE_COLLISION_WRAPPER_TIMEOUT_MS"
                    ))
                return {
                    "fbx": {"status": "ok", "kwargs": kwargs},
                    "xml": {"status": "ok", "kwargs": kwargs},
                }

            helper.speedtree_export_gate = original_gate
            helper.export_bundle = export_bundle
            output = io.StringIO()
            with mock.patch.object(
                preflight,
                "require_texture_skip_writing",
            ), mock.patch.dict(
                os.environ,
                {"SPEEDTREE_COLLISION_WRAPPER_TIMEOUT_MS": "existing"},
            ), redirect_stdout(output):
                result = preflight.run_export(args, helper)
                restored_timeout = os.environ.get(
                    "SPEEDTREE_COLLISION_WRAPPER_TIMEOUT_MS"
                )

            self.assertEqual(
                events,
                ["gate_enter", "export", "180000", "gate_exit"],
            )
            self.assertEqual(
                restored_timeout,
                "existing",
            )
            self.assertIs(helper.speedtree_export_gate, original_gate)
            self.assertEqual(result["fbx"]["status"], "ok")
            self.assertEqual(result["xml"]["status"], "ok")
            targets = result["fbx"]["kwargs"]["targets"]
            self.assertEqual([row[0] for row in targets], ["fbx", "xml"])
            text = output.getvalue()
            self.assertIn(preflight.SPEEDTREE_SLOT_WAIT_MARKER, text)
            self.assertIn(preflight.SPEEDTREE_SLOT_ACQUIRED_MARKER, text)

            events.clear()
            with mock.patch.object(
                preflight,
                "require_texture_skip_writing",
            ), mock.patch.dict(
                os.environ,
                {"SPEEDTREE_COLLISION_WRAPPER_TIMEOUT_MS": "existing"},
            ), mock.patch("builtins.print", side_effect=OSError("closed log")):
                result = preflight.run_export(args, helper)

            self.assertEqual(result["fbx"]["status"], "ok")
            self.assertEqual(
                events,
                ["gate_enter", "export", "180000", "gate_exit"],
            )
            self.assertIs(helper.speedtree_export_gate, original_gate)

    def test_raw_source_is_structured_provisional_until_pcg_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "tree_test" / "SK_tree_test.spm"
            spm.parent.mkdir()
            write_spm(spm)
            source_root = root / "Texture"
            source_root.mkdir()
            albedo = source_root / "TCom_leaf_albedo.tif"
            opacity = source_root / "TCom_leaf_opacity.tif"
            albedo.write_bytes(b"albedo")
            opacity.write_bytes(b"opacity")
            stmat = spm.parent / "fbx" / "SK_tree_test.stmat"
            stmat.parent.mkdir()
            self._write_raw_stmat(
                stmat,
                "M_leaf_grass_dead_Mat",
                {"Color": albedo, "Opacity": opacity},
            )

            result = preflight.augment_texture_readiness_contract(
                preflight.resolve_texture_bindings(stmat),
                stmat,
                spm,
                source_texture_roots=[source_root],
            )

            self.assertEqual(
                result["status"],
                "source_fallback_needs_pcg_generation",
            )
            self.assertEqual(result["missing"], [])
            warning = result["warnings"][0]
            self.assertEqual(
                set(warning["expected_t_paths"]),
                set(REQUIRED_TEXTURE_ROLES),
            )
            self.assertEqual(
                warning["expected_texture_base"],
                "T_leaf_grass_dead",
            )
            self.assertIn("PCG ST9 Texture", warning["remediation"])
            self.assertEqual(
                result["bindings"][0]["texture_contract_status"],
                "source_fallback_needs_pcg_generation",
            )
            self.assertEqual(
                result["bindings"][0]["origin_state"],
                "source_fallback_needs_pcg_generation",
            )
            issues = preflight.preflight_contract_issues({
                "texture_readiness_contract": result,
            })
            self.assertEqual(issues, [])

    def test_fbx_copy_is_blocked_instead_of_becoming_provisional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "tree_test"
            spm = asset / "SK_tree_test.spm"
            asset.mkdir()
            write_spm(spm)
            source_root = root / "Texture"
            source_root.mkdir()
            copied = asset / "fbx" / "TCom_leaf_albedo.tif"
            copied.parent.mkdir()
            copied.write_bytes(b"copied")
            stmat = asset / "fbx" / "SK_tree_test.stmat"
            self._write_raw_stmat(
                stmat,
                "M_leaf_grass_dead_Mat",
                {"Color": copied},
            )

            result = preflight.augment_texture_readiness_contract(
                preflight.resolve_texture_bindings(stmat),
                stmat,
                spm,
                source_texture_roots=[source_root],
            )

            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(
                result["missing"][0]["reason"],
                "provisional_source_blocked",
            )
            self.assertEqual(
                result["missing"][0]["source_rejections"][0]["state"],
                "blocked_cache_source",
            )
            self.assertEqual(
                result["missing"][0]["source_rejections"][0]["path"],
                str(copied.resolve()),
            )

    def test_issue64_fresh_inactive_rows_are_nonblocking_diagnostics(self):
        fixture, result, issues = self.evaluate_issue64_fixture()

        self.assertEqual(
            result["status"],
            fixture["expected"]["readiness_status"],
        )
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["warnings"]), 1)
        diagnostic = result["warnings"][0]
        self.assertEqual(
            diagnostic["issue_code"],
            fixture["expected"]["diagnostic_issue_code"],
        )
        self.assertEqual(
            diagnostic["export_scope"]["reason"],
            fixture["expected"]["diagnostic_reason"],
        )
        self.assertEqual(
            diagnostic["export_scope"]["expected_visible_material_names"],
            [],
        )
        self.assertEqual(
            {
                row["generator_type"]
                for row in diagnostic["export_scope"]["bindings"]
            },
            {"Frond", "Leaf Mesh"},
        )
        self.assertEqual(
            {
                row["slot_prefix"]
                for row in diagnostic["export_scope"]["bindings"]
            },
            {"Material:Frond:0", "Leaves:Type:0"},
        )
        binding = result["bindings"][0]
        self.assertEqual(binding["status"], "not_managed")
        self.assertEqual(
            binding["texture_contract_status"],
            "inactive_provisional_source_diagnostic",
        )
        self.assertEqual(issues, [])

    def test_issue64_live_exporting_texture_candidate_is_omitted_not_blocked(self):
        def make_live(snapshot):
            row = snapshot["leaf_generator_bindings"][0]
            row["visible"] = True
            row["graph_visible"] = True
            row["generated_node_count"] = 3
            row["export_participates"] = True

        _fixture, result, issues = self.evaluate_issue64_fixture(make_live)

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(
            result["missing"][0]["reason"],
            "provisional_source_blocked",
        )
        self.assertEqual(
            result["missing"][0]["export_scope"]["reason"],
            "material_live_binding_visible_or_exporting",
        )
        self.assertEqual(issues, [])

    def test_issue64_stale_texture_evidence_stays_telemetry_only(self):
        def make_stale(snapshot):
            snapshot["node_table"]["stale"] = True
            snapshot["node_table"]["orphan_node_count"] = 9
            for row in snapshot["leaf_generator_bindings"]:
                row["export_evidence"] = "node_table_stale"
                row["node_table_stale"] = True

        _fixture, result, issues = self.evaluate_issue64_fixture(make_stale)

        self.assertEqual(result["status"], "incomplete")
        scope = result["missing"][0]["export_scope"]
        self.assertEqual(
            scope["reason"],
            "live_export_evidence_stale_node_table",
        )
        self.assertTrue(scope["node_table"]["stale"])
        self.assertEqual(issues, [])

    def test_issue64_ambiguous_texture_binding_is_left_unassigned(self):
        def make_ambiguous(snapshot):
            del snapshot["leaf_generator_bindings"][1]["graph_visible"]

        _fixture, result, issues = self.evaluate_issue64_fixture(
            make_ambiguous
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(
            result["missing"][0]["export_scope"]["reason"],
            "material_live_binding_state_ambiguous",
        )
        self.assertEqual(issues, [])

    def test_issue64_nonblocking_diagnostic_allows_main_to_finish(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "issue64_inactive_material_preflight.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "sanitized_cluster03"
            asset.mkdir()
            spm = asset / "SK_cluster_species_03.spm"
            report_path = asset / "preflight.json"
            write_spm(spm)
            source = asset / "fbx" / "foreign_species_leaf_color.tif"
            source.parent.mkdir()
            source.write_bytes(b"foreign provisional pixels")
            stmat = asset / "fbx" / "SK_cluster_species_03.stmat"
            self._write_raw_stmat(
                stmat,
                fixture["material"]["material_name"],
                {"Color": source},
            )
            snapshot = json.loads(json.dumps(
                fixture["live_export_snapshot"]
            ))
            snapshot["spm"] = str(spm.resolve())
            inspection = {
                "materials": [{
                    "material_id": fixture["material"]["material_id"],
                    "material_name": fixture["material"]["material_name"],
                    "slots": [],
                }],
            }
            with mock.patch.object(
                preflight,
                "inspect_spm_leaf_contract",
                return_value=fixture["leaf_reference_contract"],
            ), mock.patch.object(
                preflight,
                "inspect_all_speedtree_material_export",
                return_value=fixture["all_export_material_contract"],
            ), mock.patch.object(
                preflight,
                "inspect_spm_texture_slots",
                return_value=inspection,
            ), mock.patch.object(
                preflight,
                "live_generator_delivery_snapshot",
                return_value=snapshot,
            ), mock.patch.object(
                preflight,
                "load_pcg_texture_config",
                return_value={"source_texture_roots": []},
            ):
                exited, export_mock = self.run_preflight(
                    spm,
                    report_path,
                )

            self.assertFalse(exited)
            export_mock.assert_called_once()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(
                report["texture_readiness_contract"]["status"],
                "nonblocking_diagnostics",
            )
            issues = report["speedtree_pipeline_contract"]["issues"]
            self.assertEqual(issues, [])

    def test_invalid_cluster_bake_preserves_exact_origin_issue(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "weed_test"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            spm = asset / "SK_weed_test_01.spm"
            write_spm(spm)
            color = cluster / "cluster_test_01.tga"
            subsurface = cluster / "cluster_test_01_Subsurface.tga"
            color.write_bytes(b"color")
            subsurface.write_bytes(b"subsurface")
            stmat = asset / "fbx" / "SK_weed_test_01.stmat"
            stmat.parent.mkdir()
            self._write_raw_stmat(
                stmat,
                "M_leaf_test_atlas_01_Mat",
                {
                    "Color": color,
                    "SubsurfaceColor": subsurface,
                    "SubsurfaceAmount": subsurface,
                },
            )
            slots = [
                {
                    "map_index": index,
                    "map": map_name,
                    "role": map_name.casefold(),
                    "authored_ref": f"cluster/{source.name}",
                    "resolved_ref": str(source.resolve()),
                }
                for index, (map_name, source) in enumerate(
                    (
                        ("Color", color),
                        ("SubsurfaceColor", subsurface),
                        ("SubsurfaceAmount", subsurface),
                    )
                )
            ]
            inspection = {
                "materials": [{
                    "material_id": "22",
                    "material_name": "M_leaf_test_atlas_01",
                    "slots": slots,
                }],
            }

            with mock.patch.object(
                preflight,
                "inspect_spm_texture_slots",
                return_value=inspection,
            ), mock.patch.object(
                preflight,
                "resolve_blender_cluster_bake_origin",
                return_value=(
                    {},
                    "blender_cluster_bake_map_role_mismatch",
                ),
            ):
                result = preflight.augment_texture_readiness_contract(
                    preflight.resolve_texture_bindings(stmat),
                    stmat,
                    spm,
                    source_texture_roots=[],
                )

            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(
                result["missing"][0]["reason"],
                "blender_cluster_bake_origin_invalid",
            )
            self.assertEqual(
                result["missing"][0]["origin_validation_issue"],
                "blender_cluster_bake_map_role_mismatch",
            )
            self.assertEqual(
                result["bindings"][0]["origin_validation"][
                    "classification"
                ],
                "asset_cluster_bake_texture_contract_invalid",
            )
            self.assertTrue(
                preflight._is_cluster_capture_source_set([
                    Path(temporary)
                    / "other_asset"
                    / "cluster"
                    / "cluster_shared.tga"
                ])
            )
            self.assertFalse(
                preflight._is_cluster_capture_source_set([
                    Path(temporary) / "Texture" / "leaf_source.tif"
                ])
            )

    def test_selected_manifest_proves_exact_duplicate_name_cluster_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            asset = Path(temporary) / "tree_birch_sample"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            spm = asset / "SK_tree_birch_sample_03.spm"
            spm.write_bytes(b"sanitized-spm")
            color = cluster / "leaf_main.tga"
            opacity = cluster / "leaf_main_Opacity.tga"
            color.write_bytes(b"color")
            opacity.write_bytes(b"opacity")
            stmat = asset / "fbx" / f"{spm.stem}.stmat"
            stmat.parent.mkdir()
            self._write_raw_stmat(
                stmat,
                "M_leaf_main_Mat",
                {"Color": color, "Opacity": opacity},
            )
            scope_dir = asset / ".atlas_leaf_speedtree_scopes"
            scope_dir.mkdir()
            manifest = {
                "atlas_manifest_schema_version": 1,
                "spm": str(spm),
                "blend_file": str(cluster / "SK_leaf_main.blend"),
                "source_collection": "Atlas_Cluster_Cards",
                "export_scope_id": "scope-leaf-main",
                "material_groups": [{
                    "material": "M_leaf_main",
                    "material_id": 17,
                    "mesh_ids": [35],
                    "blender_cluster_bake_texture": {
                        "files": {
                            "albedo": str(color),
                            "alpha": str(opacity),
                        },
                        "origin_receipt": {"proof": "selected-exact-target"},
                    },
                }],
                "generator_connection": {
                    "requested": True,
                    "complete": True,
                    "bindings": [],
                },
            }
            (scope_dir / f"scope-leaf-main__{spm.stem}.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            slots = [
                {
                    "map_index": index,
                    "map": map_name,
                    "role": map_name.casefold(),
                    "authored_ref": str(source),
                    "resolved_ref": str(source.resolve()),
                }
                for index, (map_name, source) in enumerate(
                    (("Color", color), ("Opacity", opacity))
                )
            ]
            inspection = {
                "materials": [
                    {
                        "material_id": "4",
                        "material_name": "leaf_main",
                        "slots": slots,
                    },
                    {
                        "material_id": "17",
                        "material_name": "M_leaf_main",
                        "slots": slots,
                    },
                ],
            }
            live_rows = [{
                "material_id": "17",
                "material_name": "M_leaf_main",
                "cutout_mesh_ids": ["35"],
                "refs": [str(color), str(opacity)],
                "managed_leaf_output": True,
            }]

            def prove_origin(
                _spm,
                material,
                output,
                _asset_root,
                *,
                consumption_context,
            ):
                self.assertEqual(material["material_id"], "17")
                self.assertEqual(
                    output["origin_receipt"]["proof"],
                    "selected-exact-target",
                )
                self.assertEqual(
                    consumption_context,
                    preflight.BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW,
                )
                return ({"slot_files": output["slot_files"]}, "")

            with mock.patch.object(
                preflight,
                "inspect_spm_texture_slots",
                return_value=inspection,
            ), mock.patch.object(
                preflight,
                "extract_material_image_refs",
                return_value=live_rows,
            ), mock.patch.object(
                preflight,
                "resolve_blender_cluster_bake_origin",
                side_effect=prove_origin,
            ), mock.patch.object(
                preflight,
                "validate_blender_cluster_bake_receipt_for_consumption",
                return_value="",
            ):
                result = preflight.augment_texture_readiness_contract(
                    preflight.resolve_texture_bindings(stmat),
                    stmat,
                    spm,
                    source_texture_roots=[],
                )

            binding = result["bindings"][0]
            self.assertEqual(
                binding["texture_contract_status"],
                "blender_cluster_bake",
            )
            self.assertEqual(
                binding["atlas_manifest_ownership"]["material_id"],
                "17",
            )
            self.assertNotIn("source_rejections", binding)

    def test_cluster_bake_receipt_records_proven_stmat_index_space(self):
        root = Path("C:/asset")
        color = root / "cluster" / "color.tga"
        normal = root / "cluster" / "normal.tga"
        receipt = {
            "slot_files": [
                {
                    "map_index": 0,
                    "map": "Color",
                    "path": str(color),
                    "sha256": "a" * 64,
                },
                {
                    "map_index": 1,
                    "map": "Normal",
                    "path": str(normal),
                    "sha256": "b" * 64,
                },
            ],
        }
        spm_slots = [
            {
                "map_index": 0,
                "map": "Color",
                "role": "color",
                "resolved_ref": str(color),
            },
            {
                "map_index": 1,
                "map": "Normal",
                "role": "normal",
                "resolved_ref": str(normal),
            },
        ]
        stmat_sources = [
            {
                "map_index": 4,
                "map": "Normal",
                "resolved_source": str(normal),
            },
            {
                "map_index": 7,
                "map": "Color",
                "resolved_source": str(color),
            },
        ]

        normalized = (
            preflight._cluster_bake_receipt_with_explicit_index_space(
                receipt,
                spm_slots,
                stmat_sources,
            )
        )

        self.assertEqual(
            normalized["slot_index_space"],
            preflight.STMAT_MAP_INDEX_SPACE,
        )
        self.assertEqual(
            [
                (
                    row["map"],
                    row["map_index"],
                    row["stmat_map_index"],
                    row["spm_map_index"],
                    row["sha256"],
                )
                for row in normalized["slot_files"]
            ],
            [
                ("Color", 7, 7, 0, "a" * 64),
                ("Normal", 4, 4, 1, "b" * 64),
            ],
        )

    def test_duplicate_stmat_map_name_keeps_source_spm_index_space(self):
        root = Path("C:/asset")
        color = root / "cluster" / "color.tga"
        receipt = {
            "slot_files": [{
                "map_index": 2,
                "map": "Color",
                "path": str(color),
                "sha256": "a" * 64,
            }],
        }
        spm_slots = [{
            "map_index": 2,
            "map": "Color",
            "role": "color",
            "resolved_ref": str(color),
        }]
        stmat_sources = [
            {
                "map_index": 0,
                "map": "Color",
                "resolved_source": str(color),
            },
            {
                "map_index": 1,
                "map": "Color",
                "resolved_source": str(color),
            },
        ]

        normalized = (
            preflight._cluster_bake_receipt_with_explicit_index_space(
                receipt,
                spm_slots,
                stmat_sources,
            )
        )

        self.assertEqual(
            normalized["slot_index_space"],
            preflight.SOURCE_SPM_MAP_INDEX_SPACE,
        )
        self.assertEqual(
            normalized["slot_files"][0]["map_index"],
            2,
        )
        self.assertEqual(
            normalized["slot_files"][0]["spm_map_index"],
            2,
        )
        self.assertNotIn(
            "stmat_map_index",
            normalized["slot_files"][0],
        )

    @staticmethod
    def _write_raw_stmat(path, material_name, maps):
        root = ET.Element("SpeedTreeMaterials")
        material = ET.SubElement(
            root,
            "Material",
            ID="1",
            Name=material_name,
        )
        for map_name, source in maps.items():
            ET.SubElement(
                material,
                "Map",
                Name=map_name,
                Source=str(source),
            )
        ET.ElementTree(root).write(
            path,
            encoding="utf-8",
            xml_declaration=True,
        )

    def test_marker_only_atlas_ownership_is_silent_mutation_diagnostic(self):
        issues = preflight.preflight_contract_issues({
            "spm": "SK_atlas.spm",
            "leaf_reference_contract": {
                "status": "managed_connected",
                "managed_ownership_provenance": {
                    "status": "marker_only",
                    "material_names": ["M_leaf_atlas_green"],
                    "reason": "strict ownership was not proven",
                },
            },
        })

        self.assertEqual(issues, [])

    def test_provider_claim_disagreement_does_not_abort_texture_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_overlapping_providers.spm"
            spm.write_bytes(b"fixture-spm")
            authority = {
                "atlas_manifest_schema_version": 1,
                "spm": str(spm),
                "blend_file": str(root / "provider_a.blend"),
                "source_collection": "Provider A",
                "export_scope_id": "provider-a",
                "material_groups": [{
                    "material": "M_leaf_shared",
                    "material_id": 7,
                    "mesh_ids": [20],
                }],
                "generator_connection": {
                    "complete": True,
                    "bindings": [],
                },
            }
            target_dir = root / ".atlas_leaf_speedtree_targets"
            target_dir.mkdir()
            (target_dir / f"{spm.stem}.json").write_text(
                json.dumps(authority),
                encoding="utf-8",
            )
            competing = json.loads(json.dumps(authority))
            competing["blend_file"] = str(root / "provider_b.blend")
            competing["source_collection"] = "Provider B"
            competing["export_scope_id"] = "provider-b"
            competing["material_groups"][0]["mesh_ids"] = [99]
            (root / "speedtree_import_manifest.json").write_text(
                json.dumps(competing),
                encoding="utf-8",
            )
            with mock.patch.object(
                preflight,
                "read_stmat_material_sources",
                return_value={"materials": []},
            ), mock.patch.object(
                preflight,
                "inspect_spm_texture_slots",
                return_value={"materials": []},
            ), mock.patch.object(
                preflight,
                "extract_material_image_refs",
                return_value=[],
            ):
                readiness = preflight.augment_texture_readiness_contract(
                    {
                        "bindings": [{
                            "material": "M_leaf_shared",
                            "material_index": 0,
                            "status": "not_managed",
                        }],
                        "warnings": [],
                        "missing": [],
                    },
                    root / "SK_overlapping_providers.stmat",
                    spm,
                )

            diagnostic = readiness["atlas_manifest_diagnostic"]
            self.assertEqual(
                diagnostic["status"],
                "provider_claim_disagreement",
            )
            self.assertFalse(diagnostic["mutation_authorized"])
            self.assertTrue(diagnostic["resolution"]["conflicting"])
            self.assertEqual(readiness["warnings"], [])
            self.assertEqual(readiness["missing"], [])

    def run_preflight(self, spm, report_path):
        args = argparse.Namespace(
            spm=str(spm),
            speedtree_exe="SpeedTree.exe",
            fbx_ini="Options.ini",
            speedtree_cli="speedtree_cli.py",
            report=str(report_path),
            timeout=30,
        )
        exited = False
        with mock.patch.object(preflight, "parse_args", return_value=args), mock.patch.object(
            preflight, "load_speedtree_cli", return_value=object()
        ), mock.patch.object(
            preflight,
            "run_export",
            return_value={"status": "cached", "exists": True, "size": 1},
        ) as export_mock:
            try:
                preflight.main()
            except SystemExit:
                exited = True
        return exited, export_mock

    def test_report_contains_versioned_sources_and_authoritative_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_grass.spm"
            report_path = root / "report.json"
            write_spm(spm)
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            self.run_preflight(spm, report_path)

            report = json.loads(report_path.read_text(encoding="utf-8"))
            envelope = report["speedtree_pipeline_contract"]
            self.assertEqual(report["status"], "ok")
            self.assertEqual(envelope["outcome"], "ok")
            self.assertRegex(envelope["source"]["spm"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                envelope["source"]["stmat"][0]["sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertEqual(len(envelope["material_intents"]), 2)
            self.assertTrue(
                all(
                    intent["texture_binding"]["texture_base"]
                    == "T_grass_shared"
                    for intent in envelope["material_intents"]
                )
            )
            self.assertTrue(
                all(
                    "stmat_roles" in intent["texture_binding"]
                    for intent in envelope["material_intents"]
                )
            )

    def test_main_returns_success_with_structured_provisional_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "tree_test"
            asset.mkdir()
            spm = asset / "SK_tree_test.spm"
            report_path = root / "report.json"
            write_spm(spm)
            source_root = root / "Texture"
            source_root.mkdir()
            albedo = source_root / "TCom_leaf_albedo.tif"
            albedo.write_bytes(b"albedo")
            stmat = asset / "fbx" / "SK_tree_test.stmat"
            stmat.parent.mkdir()
            document = ET.Element("SpeedTreeMaterials")
            for index, name in enumerate(
                ("M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"),
                1,
            ):
                material = ET.SubElement(
                    document,
                    "Material",
                    ID=str(index),
                    Name=name,
                )
                ET.SubElement(
                    material,
                    "Map",
                    Name="Color",
                    Source=str(albedo),
                )
            ET.ElementTree(document).write(
                stmat,
                encoding="utf-8",
                xml_declaration=True,
            )
            now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
            os.utime(spm, ns=(now, now))
            os.utime(stmat, ns=(now + 1, now + 1))

            with mock.patch.object(
                preflight,
                "load_pcg_texture_config",
                return_value={
                    "source_texture_roots": [str(source_root)]
                },
            ):
                exited, export_mock = self.run_preflight(
                    spm,
                    report_path,
                )

            self.assertFalse(exited)
            export_mock.assert_called_once()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            readiness = report["texture_readiness_contract"]
            self.assertEqual(
                readiness["status"],
                "source_fallback_needs_pcg_generation",
            )
            self.assertEqual(len(readiness["warnings"]), 2)
            issues = report["speedtree_pipeline_contract"]["issues"]
            self.assertEqual(
                {
                    issue["code"] for issue in issues
                    if issue["severity"] == "warning"
                },
                set(),
            )
            self.assertTrue(all(
                set(warning["expected_t_paths"])
                == set(REQUIRED_TEXTURE_ROLES)
                for warning in readiness["warnings"]
            ))

    def test_missing_mesh_file_blocks_before_speedtree_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_fern_missing_mesh.spm"
            report_path = root / "report.json"
            mesh_dir = root / "meshes"
            mesh_dir.mkdir()
            (mesh_dir / "01_leaf_present.fbx").write_bytes(b"fbx")
            write_spm(
                spm,
                mesh_filenames=(
                    "meshes/01_leaf_present.fbx",
                    "meshes/18_leaf_gone.fbx",
                ),
                referenced_external_mesh_indexes=(1,),
            )
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exited, export_mock = self.run_preflight(spm, report_path)

            self.assertTrue(exited)
            export_mock.assert_not_called()
            progress = output.getvalue()
            self.assertIn(preflight.MATERIAL_PREFLIGHT_START_MARKER, progress)
            self.assertIn(
                preflight.MATERIAL_PREFLIGHT_STATIC_DONE_MARKER,
                progress,
            )
            self.assertIn(
                preflight.MATERIAL_PREFLIGHT_CONTRACT_DONE_MARKER,
                progress,
            )
            self.assertIn(preflight.MATERIAL_PREFLIGHT_DONE_MARKER, progress)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            envelope = report["speedtree_pipeline_contract"]
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(envelope["outcome"], "blocked")
            self.assertIn("18_leaf_gone.fbx", report["error"])
            self.assertEqual(
                report["classification"],
                "asset_external_mesh_path_missing",
            )
            self.assertIn("relink", report["remediation"].casefold())
            self.assertEqual(
                report["missing_external_meshes"][0]["filename"],
                "meshes/18_leaf_gone.fbx",
            )
            self.assertNotIn("speedtree_export", report)
            self.assertIn(
                "SPM_MESH_FILE_MISSING",
                {issue["code"] for issue in envelope["issues"]},
            )
            contract = report["mesh_file_reference_contract"]
            self.assertEqual(contract["status"], "missing_mesh_files")
            self.assertEqual(
                [row["filename"] for row in contract["missing"]],
                ["meshes/18_leaf_gone.fbx"],
            )

    def test_missing_orphan_mesh_file_is_reported_but_does_not_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_fern_orphan_mesh.spm"
            report_path = root / "report.json"
            write_spm(
                spm,
                mesh_filenames=("meshes/old_atlas_plate.fbx",),
            )
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            exited, export_mock = self.run_preflight(spm, report_path)

            self.assertFalse(exited)
            export_mock.assert_called_once()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            contract = report["mesh_file_reference_contract"]
            self.assertEqual(
                contract["status"],
                "orphan_missing_mesh_assets",
            )
            self.assertEqual(contract["missing"], [])
            self.assertEqual(
                [row["filename"] for row in contract["orphan_missing"]],
                ["meshes/old_atlas_plate.fbx"],
            )
            self.assertEqual(
                contract["orphan_missing"][0]["usage"],
                "orphan",
            )
            self.assertNotIn(
                "SPM_MESH_FILE_MISSING",
                {
                    issue["code"]
                    for issue in report["speedtree_pipeline_contract"][
                        "issues"
                    ]
                },
            )

    def test_unbound_marker_only_missing_mesh_is_not_export_authority(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_fern_managed_orphan.spm"
            report_path = root / "report.json"
            present = root / "meshes" / "present_plate.fbx"
            present.parent.mkdir()
            present.write_bytes(b"fbx")
            write_spm(
                spm,
                mesh_filenames=(
                    "meshes/present_plate.fbx",
                    "meshes/removed_plate.fbx",
                ),
                material_external_mesh_indexes=(0, 1),
                managed_leaf=True,
            )
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            exited, export_mock = self.run_preflight(spm, report_path)

            self.assertFalse(exited)
            export_mock.assert_called_once()
            contract = json.loads(
                report_path.read_text(encoding="utf-8")
            )["mesh_file_reference_contract"]
            self.assertEqual(
                contract["status"],
                "orphan_missing_mesh_assets",
            )
            self.assertEqual(contract["missing"], [])
            self.assertEqual(
                contract["orphan_missing"][0]["usage"],
                "managed_orphan",
            )
            self.assertFalse(
                contract["atlas_consumer_integrity"]["blocking"]
            )
            self.assertTrue(
                contract["atlas_consumer_integrity"]["mutation_blocking"]
            )

    def test_hidden_default_cutout_missing_mesh_does_not_block_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_hidden_default_cutout.spm"
            report_path = root / "report.json"
            write_spm(
                spm,
                mesh_filenames=("meshes/missing_hidden_plate.fbx",),
                material_external_mesh_indexes=(0,),
                managed_leaf=True,
                leaf_hidden=True,
                leaf_default_cutout=True,
            )
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            exited, export_mock = self.run_preflight(spm, report_path)

            self.assertFalse(exited)
            export_mock.assert_called_once()
            contract = json.loads(
                report_path.read_text(encoding="utf-8")
            )["mesh_file_reference_contract"]
            self.assertEqual(contract["status"], "orphan_missing_mesh_assets")
            self.assertEqual(contract["missing"], [])
            self.assertEqual(
                [row["filename"] for row in contract["orphan_missing"]],
                ["meshes/missing_hidden_plate.fbx"],
            )

    def test_unbound_final_managed_mesh_is_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_final_managed_mesh.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            material = ET.SubElement(
                assets,
                "Material_v8",
                ID="1",
                Name="M_leaf_atlas",
            )
            ET.SubElement(material, "CutoutMeshID").text = "90"
            ET.SubElement(material, "UserData").text = json.dumps(
                {
                    "generator": "Atlas Leaf Mesh Builder",
                    "scope": "test-scope",
                    "kind": "material",
                }
            )
            mesh = ET.SubElement(
                assets,
                "Mesh",
                ID="90",
                Name="last_plate",
            )
            ET.SubElement(mesh, "Filename").text = (
                "meshes/missing_last_plate.fbx"
            )
            ET.SubElement(mesh, "Embedded").text = "false"
            spm.write_bytes(
                gzip.compress(ET.tostring(model, encoding="utf-8"))
            )

            contract = inspect_spm_mesh_file_references(spm)

            self.assertEqual(contract["status"], "orphan_missing_mesh_assets")
            self.assertEqual(
                [row["mesh_id"] for row in contract["orphan_missing"]],
                [90],
            )
            self.assertEqual(
                contract["orphan_missing"][0]["usage"],
                "managed_orphan",
            )

    def test_existing_mesh_files_do_not_block_the_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_fern_meshes_ok.spm"
            report_path = root / "report.json"
            mesh_dir = root / "meshes"
            mesh_dir.mkdir()
            (mesh_dir / "01_leaf_present.fbx").write_bytes(b"fbx")
            write_spm(spm, mesh_filenames=("meshes/01_leaf_present.fbx",))
            write_stmat(
                spm,
                ["M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"],
            )

            exited, export_mock = self.run_preflight(spm, report_path)

            self.assertFalse(exited)
            export_mock.assert_called_once()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(
                report["mesh_file_reference_contract"]["status"], "ok"
            )

    def test_missing_visible_stem_blocks_with_all_export_issue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_grass_missing_stem.spm"
            report_path = root / "report.json"
            write_spm(spm)
            write_stmat(spm, ["M_leaf_grass_dead_Mat"])
            source_before = spm.read_bytes()

            exited, _export_mock = self.run_preflight(spm, report_path)

            self.assertTrue(exited)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            envelope = report["speedtree_pipeline_contract"]
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(envelope["outcome"], "blocked")
            self.assertIn(
                "ALL_EXPORT_MATERIAL_MISSING",
                {issue["code"] for issue in envelope["issues"]},
            )
            self.assertEqual(
                report["all_export_material_contract"]["missing_materials"],
                ["M_stem_common_01"],
            )
            self.assertEqual(
                report["classification"],
                "asset_export_material_missing",
            )
            self.assertEqual(
                report["missing_export_materials"],
                ["M_stem_common_01"],
            )
            self.assertIn("assign", report["remediation"].casefold())
            self.assertEqual(spm.read_bytes(), source_before)
            self.assertFalse(report["problem_node_marker"]["changed"])
            self.assertEqual(
                report["problem_node_marker"]["status"], "reported_only"
            )

    def test_textureless_stmat_continues_after_material_name_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_reed_textureless.spm"
            report_path = root / "report.json"
            write_spm(spm)
            stmat = root / "fbx" / "SK_reed_textureless.stmat"
            stmat.parent.mkdir(parents=True, exist_ok=True)
            document = ET.Element("SpeedTreeMaterials")
            for index, name in enumerate(
                ("M_leaf_grass_dead_Mat", "M_stem_common_01_Mat"), 1
            ):
                ET.SubElement(
                    document, "Material", ID=str(index), Name=name
                )
            ET.ElementTree(document).write(
                stmat, encoding="utf-8", xml_declaration=True
            )
            now = max(spm.stat().st_mtime_ns, stmat.stat().st_mtime_ns)
            os.utime(spm, ns=(now, now))
            os.utime(stmat, ns=(now + 1, now + 1))

            exited, export_mock = self.run_preflight(spm, report_path)

            self.assertFalse(exited)
            export_mock.assert_called_once()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(
                report["speedtree_pipeline_contract"]["outcome"], "ok"
            )
            self.assertEqual(
                report["texture_source_contract"]["status"],
                "ok",
            )
            self.assertEqual(
                report["texture_source_contract"]["availability_status"],
                "textureless",
            )
            self.assertFalse(
                report["texture_source_contract"]["affects_pipeline_outcome"]
            )
            self.assertEqual(report["speedtree_pipeline_contract"]["issues"], [])

if __name__ == "__main__":
    unittest.main()
