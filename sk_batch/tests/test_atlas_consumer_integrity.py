import argparse
import gzip
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
JOBS_DIR = SK_BATCH_DIR / "jobs"
import sys

for import_path in (SK_BATCH_DIR, JOBS_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from spm_leaf_handoff_contract import inspect_spm_mesh_file_references  # noqa: E402
import speedtree_material_preflight as preflight  # noqa: E402


ATLAS_MARKER = "Atlas Leaf Mesh Builder"
SILKY_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "issue57_silky_managed_orphans.json"
)


def marker(kind, scope, group="leaf"):
    return json.dumps({
        "generator": ATLAS_MARKER,
        "kind": kind,
        "scope": scope,
        "group": group,
    })


def add_material(
    assets,
    material_id,
    name,
    mesh_ids,
    scope=None,
    *,
    group="leaf",
):
    material = ET.SubElement(
        assets,
        "Material_v8",
        ID=str(material_id),
        Name=name,
    )
    ET.SubElement(material, "CutoutMeshID").text = str(mesh_ids[0])
    supplemental = ET.SubElement(material, "SupplementalCutoutMeshIDs")
    for mesh_id in mesh_ids[1:]:
        ET.SubElement(supplemental, "CutoutMesh", ID=str(mesh_id))
    if scope is not None:
        ET.SubElement(material, "UserData").text = marker(
            "material", scope, group
        )
    return material


def add_external_mesh(
    assets,
    root,
    mesh_id,
    scope=None,
    exists=True,
    *,
    group="leaf",
):
    mesh = ET.SubElement(
        assets,
        "Mesh",
        ID=str(mesh_id),
        Name=f"mesh_{mesh_id}",
    )
    ET.SubElement(mesh, "Embedded").text = "false"
    filename = f"meshes/mesh_{mesh_id}.fbx"
    ET.SubElement(mesh, "Filename").text = filename
    if scope is not None:
        ET.SubElement(mesh, "UserData").text = marker("mesh", scope, group)
    if exists:
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"mesh:{mesh_id}".encode("ascii"))


def add_generator(
    model,
    material_id,
    mesh_id,
    index=0,
    *,
    guid=True,
    hidden=False,
):
    generator = ET.SubElement(model, "Generator", Type="Leaf Mesh")
    if guid:
        ET.SubElement(generator, "GUID").text = f"leaf-guid-{index}"
    ET.SubElement(generator, "Name").text = f"Leaf {index}"
    if hidden:
        ET.SubElement(generator, "Hidden").text = "true"
    properties = ET.SubElement(generator, "Properties")
    for suffix, value in (("Material", material_id), ("Mesh", mesh_id)):
        prop = ET.SubElement(properties, "Property")
        ET.SubElement(prop, "Name").text = f"Leaves:Type:{index}:{suffix}"
        ET.SubElement(prop, "Value").text = str(value)


def write_spm(path, model):
    path.write_bytes(gzip.compress(ET.tostring(model, encoding="utf-8"), mtime=0))


def write_receipt(
    target,
    directory,
    filename,
    scope,
    material_id,
    material_name,
    mesh_ids,
    *,
    blend="C:/sources/atlas.blend",
    collection="AtlasLeaves",
    groups=None,
    lifecycle=None,
    declared_spm=None,
):
    root = target.parent / directory
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    payload = {
        "spm": str(declared_spm or target),
        "export_scope_id": scope,
        "blend_file": blend,
        "source_collection": collection,
        "speedtree_material_groups": groups or [{
            "collection": collection,
            "material": material_name,
            "material_id": material_id,
            "mesh_ids": list(mesh_ids),
        }],
        "generator_connection": {"complete": True},
    }
    if lifecycle is not None:
        payload["atlas_scope_lifecycle"] = dict(lifecycle)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class AtlasConsumerIntegrityTests(unittest.TestCase):
    def test_silky_4_active_and_99_managed_orphans_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = json.loads(SILKY_FIXTURE.read_text(encoding="utf-8"))
            root = Path(temporary)
            spm = root / fixture["target_name"]
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            generation_groups = {}
            for generation in fixture["generations"]:
                groups = []
                for group in generation["groups"]:
                    mesh_ids = list(range(
                        group["mesh_start"],
                        group["mesh_start"] + group["mesh_count"],
                    ))
                    groups.append({
                        "collection": group["group"],
                        "material": group["material_name"],
                        "material_id": group["material_id"],
                        "mesh_ids": mesh_ids,
                    })
                    add_material(
                        assets,
                        group["material_id"],
                        group["material_name"],
                        mesh_ids,
                        generation["scope_id"],
                        group=group["group"],
                    )
                    for mesh_id in mesh_ids:
                        add_external_mesh(
                            assets,
                            root,
                            mesh_id,
                            generation["scope_id"]
                            if generation["direct_mesh_markers"]
                            else None,
                            group=group["group"],
                        )
                generation_groups[generation["name"]] = groups

            active_generation = next(
                generation
                for generation in fixture["generations"]
                if generation["name"] == "SK_cluster_active_scope"
            )
            for index, mesh_id in enumerate(fixture["active_mesh_ids"]):
                add_generator(
                    model,
                    active_generation["groups"][0]["material_id"],
                    mesh_id,
                    index,
                )
            write_spm(spm, model)

            selected_manifests = {}
            current_manifest = None
            for generation in fixture["generations"]:
                if not generation["operational"]:
                    continue
                groups = generation_groups[generation["name"]]
                first_group = groups[0]
                selected_manifests[generation["name"]] = write_receipt(
                    spm,
                    ".atlas_leaf_speedtree_scopes",
                    f"{generation['scope_id']}__{spm.stem}.json",
                    generation["scope_id"],
                    first_group["material_id"],
                    first_group["material"],
                    first_group["mesh_ids"],
                    blend=generation["blend_file"],
                    collection=generation["source_collection"],
                    groups=groups,
                )
                if generation.get("per_target_mirror"):
                    current_manifest = write_receipt(
                        spm,
                        ".atlas_leaf_speedtree_targets",
                        f"{spm.stem}.json",
                        generation["scope_id"],
                        first_group["material_id"],
                        first_group["material"],
                        first_group["mesh_ids"],
                        blend=generation["blend_file"],
                        collection=generation["source_collection"],
                        groups=groups,
                    )
            legacy = next(
                generation for generation in fixture["generations"]
                if generation["name"] == "legacy_groupless_leaf"
            )
            legacy_group = generation_groups[legacy["name"]][0]
            write_receipt(
                spm,
                ".",
                "speedtree_import_manifest_M_leaf_silky_dogwood_atlas_01.json",
                legacy["scope_id"],
                legacy_group["material_id"],
                legacy_group["material"],
                legacy_group["mesh_ids"],
                blend="",
                collection="M_leaf_silky_dogwood_atlas_01",
                groups=[legacy_group],
            )
            before = spm.read_bytes()

            first = inspect_spm_mesh_file_references(spm)
            second = inspect_spm_mesh_file_references(spm)

            self.assertEqual(first, second)
            self.assertEqual(spm.read_bytes(), before)
            self.assertEqual(first["status"], "managed_asset_integrity_error")
            self.assertEqual(first["missing"], [])
            self.assertEqual(first["orphan_missing"], [])
            self.assertEqual(
                Counter(row["usage"] for row in first["references"]),
                Counter({
                    "active": fixture["expected"]["active"],
                    "managed_orphan": fixture["expected"]["managed_orphan"],
                }),
            )
            integrity = first["atlas_consumer_integrity"]
            self.assertTrue(integrity["blocking"])
            self.assertEqual(
                integrity["active_managed_mesh_count"],
                fixture["expected"]["active"],
            )
            self.assertEqual(
                integrity["managed_orphan_mesh_count"],
                fixture["expected"]["managed_orphan"],
            )
            self.assertEqual(
                integrity["classification_counts"],
                fixture["expected"]["classification_counts"],
            )
            self.assertEqual(
                len(integrity["selected_scope_ids"]),
                fixture["expected"]["selected_source_scopes"],
            )
            self.assertEqual(
                len(integrity["selected_manifest_paths"]),
                fixture["expected"]["selected_manifest_paths"],
            )
            self.assertEqual(
                integrity["current_manifest_path"].casefold(),
                str(current_manifest).casefold(),
            )
            legacy_mesh = next(
                row for row in integrity["managed_meshes"] if row["mesh_id"] == 7
            )
            self.assertEqual(
                legacy_mesh["classification"],
                "ambiguous",
            )
            self.assertEqual(legacy_mesh["orphan_reason"], "lineage_unproven")
            self.assertFalse(legacy_mesh["automatic_action_eligible"])
            authoritative = {
                row["mesh_id"]: row
                for row in integrity["managed_meshes"]
                if row["mesh_id"] in {51, 62}
            }
            self.assertEqual(
                {
                    row["orphan_reason"] for row in authoritative.values()
                },
                {"authoritative_current_unreferenced"},
            )
            self.assertFalse(any(
                row["automatic_action_eligible"]
                for row in authoritative.values()
            ))
            self.assertEqual(
                set(authoritative),
                {51, 62},
            )
            self.assertTrue({
                str(path).casefold()
                for path in selected_manifests.values()
            }.issubset({
                path.casefold()
                for generation in integrity["generations"]
                for path in generation["manifest_paths"]
            }))
            repair = integrity["repair_input"]
            self.assertEqual(len(repair["content_sha256"]), 64)
            self.assertEqual(repair, second["atlas_consumer_integrity"]["repair_input"])
            self.assertFalse(any(
                candidate["automatic_action_eligible"]
                for candidate in repair["candidates"]
            ))
            issues = preflight.preflight_contract_issues({
                "spm": str(spm),
                "mesh_file_reference_contract": first,
            })
            self.assertIn(
                "ATLAS_MANAGED_ASSET_INTEGRITY_STALE",
                {issue["code"] for issue in issues},
            )

    def test_current_default_cutout_generation_is_not_orphaned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_default_cutout.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_current", [1, 2], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_external_mesh(assets, root, 2, "scope-current")
            add_generator(model, 10, -10)
            write_spm(spm, model)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_default_cutout.json",
                "scope-current",
                10,
                "M_leaf_current",
                [1, 2],
            )

            report = inspect_spm_mesh_file_references(spm)

            integrity = report["atlas_consumer_integrity"]
            self.assertEqual(report["status"], "ok")
            self.assertFalse(integrity["blocking"])
            self.assertEqual(
                {row["usage"] for row in report["references"]},
                {"active"},
            )
            self.assertEqual(
                {row["classification"] for row in integrity["managed_materials"]},
                {"current_default_cutout"},
            )
            self.assertEqual(
                {row["classification"] for row in integrity["managed_meshes"]},
                {"current_default_cutout"},
            )

    def test_complete_unselected_groups_from_one_current_authority_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_cluster_blackgum_pattern.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            groups = [
                {
                    "collection": "Green",
                    "material": "M_cluster_green",
                    "material_id": 8,
                    "mesh_ids": [1, 2],
                },
                {
                    "collection": "stem",
                    "material": "M_cluster_stem",
                    "material_id": 9,
                    "mesh_ids": [3, 4],
                },
                {
                    "collection": "Small",
                    "material": "M_cluster_small",
                    "material_id": 10,
                    "mesh_ids": [5, 6],
                },
            ]
            for group in groups:
                add_material(
                    assets,
                    group["material_id"],
                    group["material"],
                    group["mesh_ids"],
                    "scope-current",
                    group=group["collection"],
                )
                for mesh_id in group["mesh_ids"]:
                    add_external_mesh(
                        assets,
                        root,
                        mesh_id,
                        "scope-current",
                        group=group["collection"],
                    )
            add_generator(model, 8, -10)
            write_spm(spm, model)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_cluster_blackgum_pattern.json",
                "scope-current",
                8,
                "M_cluster_green",
                [1, 2],
                groups=groups,
            )
            before = spm.read_bytes()

            report = inspect_spm_mesh_file_references(spm)

            self.assertEqual(spm.read_bytes(), before)
            self.assertEqual(report["status"], "ok")
            integrity = report["atlas_consumer_integrity"]
            self.assertFalse(integrity["blocking"])
            self.assertEqual(integrity["managed_orphan_mesh_count"], 0)
            self.assertEqual(integrity["managed_orphan_material_count"], 0)
            self.assertEqual(
                integrity["classification_counts"],
                {
                    "current_default_cutout": 3,
                    "current_unused_group": 6,
                },
            )
            self.assertEqual(
                {
                    row["material_id"]: row["classification"]
                    for row in integrity["managed_materials"]
                },
                {
                    8: "current_default_cutout",
                    9: "current_unused_group",
                    10: "current_unused_group",
                },
            )
            self.assertEqual(
                {
                    row["usage"] for row in report["references"]
                    if row["mesh_id"] in {3, 4, 5, 6}
                },
                {"current_unused_group"},
            )
            self.assertEqual(integrity["repair_input"]["candidates"], [])

    def test_foreign_scope_and_untagged_manual_assets_are_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_foreign_manual.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_current", [1], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_material(assets, 20, "M_leaf_foreign", [2], "scope-foreign")
            add_external_mesh(assets, root, 2, "scope-foreign")
            # A manual asset with the same name is still manual; names never
            # substitute for an Atlas ownership marker.
            add_material(assets, 30, "M_leaf_foreign", [3], None)
            add_external_mesh(assets, root, 3, None)
            add_generator(model, 10, 1)
            write_spm(spm, model)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_foreign_manual.json",
                "scope-current",
                10,
                "M_leaf_current",
                [1],
            )
            foreign_manifest = write_receipt(
                spm,
                ".atlas_leaf_speedtree_scopes",
                "scope-foreign.json",
                "scope-foreign",
                20,
                "M_leaf_foreign",
                [2],
                blend="C:/other/foreign.blend",
                collection="ForeignLeaves",
                declared_spm=root / "SK_other_target.spm",
            )

            report = inspect_spm_mesh_file_references(spm)

            integrity = report["atlas_consumer_integrity"]
            self.assertEqual(report["status"], "ok")
            self.assertFalse(integrity["blocking"])
            foreign_mesh = next(
                row for row in integrity["managed_meshes"] if row["mesh_id"] == 2
            )
            self.assertEqual(foreign_mesh["classification"], "protected_foreign")
            self.assertEqual(
                [path.casefold() for path in foreign_mesh["manifest_paths"]],
                [str(foreign_manifest).casefold()],
            )
            self.assertEqual(
                integrity["protected_manual_materials"][0]["material_id"],
                30,
            )
            self.assertEqual(
                integrity["protected_manual_meshes"][0]["mesh_id"],
                3,
            )
            self.assertEqual(
                {
                    row["mesh_id"]: row["usage"]
                    for row in report["references"]
                    if row["mesh_id"] in {2, 3}
                },
                {2: "protected_foreign", 3: "protected_manual"},
            )

    def test_missing_file_status_remains_separate_from_integrity_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_missing_and_stale.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_current", [1, 2], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_external_mesh(assets, root, 2, "scope-current", exists=False)
            add_generator(model, 10, 1)
            write_spm(spm, model)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_missing_and_stale.json",
                "scope-current",
                10,
                "M_leaf_current",
                [1, 2],
            )

            report = inspect_spm_mesh_file_references(spm)

            self.assertEqual(report["status"], "orphan_missing_mesh_assets")
            self.assertEqual(report["missing"], [])
            self.assertEqual(len(report["orphan_missing"]), 1)
            self.assertTrue(report["atlas_consumer_integrity"]["blocking"])

    def test_preflight_blocks_before_export_and_emits_repair_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_integrity_preflight.spm"
            report_path = root / "report.json"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_current", [1, 2], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_external_mesh(assets, root, 2, "scope-current")
            add_generator(model, 10, 1)
            write_spm(spm, model)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_integrity_preflight.json",
                "scope-current",
                10,
                "M_leaf_current",
                [1, 2],
            )
            args = argparse.Namespace(
                spm=str(spm),
                speedtree_exe="SpeedTree.exe",
                fbx_ini="Options.ini",
                speedtree_cli="speedtree_cli.py",
                report=str(report_path),
                timeout=30,
            )

            with mock.patch.object(
                preflight,
                "parse_args",
                return_value=args,
            ), mock.patch.object(
                preflight,
                "read_tree_instance_profile",
                return_value="test-profile",
            ), mock.patch.object(
                preflight,
                "load_speedtree_cli",
                return_value=object(),
            ), mock.patch.object(preflight, "run_export") as export:
                with self.assertRaises(SystemExit):
                    preflight.main()

            export.assert_not_called()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                report["classification"],
                "atlas_managed_asset_integrity_stale",
            )
            self.assertEqual(
                len(report["atlas_consumer_repair_input"]["content_sha256"]),
                64,
            )
            self.assertIn(
                "ATLAS_MANAGED_ASSET_INTEGRITY_STALE",
                {
                    issue["code"]
                    for issue in report["speedtree_pipeline_contract"]["issues"]
                },
            )

    def test_manifest_receipt_change_invalidates_cached_integrity_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_receipt_refresh.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_current", [1], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_generator(model, 10, 1)
            write_spm(spm, model)

            unresolved = inspect_spm_mesh_file_references(spm)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_receipt_refresh.json",
                "scope-current",
                10,
                "M_leaf_current",
                [1],
            )
            resolved = inspect_spm_mesh_file_references(spm)

            self.assertEqual(unresolved["status"], "managed_asset_integrity_error")
            self.assertEqual(
                unresolved["atlas_consumer_integrity"]["receipt_resolution"],
                "missing",
            )
            self.assertEqual(resolved["status"], "ok")
            self.assertEqual(
                resolved["atlas_consumer_integrity"]["receipt_resolution"],
                "resolved",
            )

    def test_duplicate_operational_ownership_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_conflicting_authority.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_current", [1], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_generator(model, 10, 1)
            write_spm(spm, model)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_conflicting_authority.json",
                "scope-current",
                10,
                "M_leaf_current",
                [1],
            )
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_scopes",
                "scope-other__SK_conflicting_authority.json",
                "scope-other",
                10,
                "M_leaf_current",
                [1],
                blend="C:/sources/other.blend",
                collection="OtherLeaves",
            )
            before = spm.read_bytes()

            integrity = inspect_spm_mesh_file_references(spm)[
                "atlas_consumer_integrity"
            ]

            self.assertEqual(spm.read_bytes(), before)
            self.assertTrue(integrity["blocking"])
            self.assertEqual(integrity["receipt_resolution"], "conflict")
            self.assertIn(
                "atlas_manifest_resolution_conflict",
                {row["code"] for row in integrity["integrity_issues"]},
            )
            self.assertEqual(
                {row["classification"] for row in integrity["managed_meshes"]},
                {"ambiguous"},
            )

    def test_hidden_generator_superseded_mesh_reference_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_hidden_superseded.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_current", [1], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_material(assets, 20, "M_leaf_old", [2], "scope-old")
            add_external_mesh(assets, root, 2, "scope-old")
            add_generator(model, 10, 1, index=0)
            add_generator(model, 20, 2, index=1, hidden=True)
            write_spm(spm, model)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_hidden_superseded.json",
                "scope-current",
                10,
                "M_leaf_current",
                [1],
            )
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_scopes",
                "scope-old__SK_hidden_superseded.json",
                "scope-old",
                20,
                "M_leaf_old",
                [2],
                lifecycle={
                    "state": "retired",
                    "successor_export_scope_id": "scope-current",
                },
            )

            integrity = inspect_spm_mesh_file_references(spm)[
                "atlas_consumer_integrity"
            ]

            old_mesh = next(
                row for row in integrity["managed_meshes"]
                if row["mesh_id"] == 2
            )
            self.assertEqual(
                old_mesh["classification"],
                "superseded_with_proven_successor",
            )
            self.assertEqual(
                [row["generator_guid"] for row in old_mesh["generator_references"]],
                ["leaf-guid-1"],
            )
            self.assertTrue(old_mesh["generator_references"][0]["hidden"])
            self.assertTrue(integrity["blocking"])

    def test_cross_group_material_mesh_pair_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_cross_group.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_a", [1], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_material(assets, 20, "M_leaf_b", [2], "scope-current")
            add_external_mesh(assets, root, 2, "scope-current")
            add_generator(model, 10, 2)
            write_spm(spm, model)
            receipt = write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_cross_group.json",
                "scope-current",
                10,
                "M_leaf_a",
                [1],
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            payload["speedtree_material_groups"].append({
                "collection": "AtlasLeavesB",
                "material": "M_leaf_b",
                "material_id": 20,
                "mesh_ids": [2],
            })
            receipt.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            integrity = inspect_spm_mesh_file_references(spm)[
                "atlas_consumer_integrity"
            ]

            self.assertTrue(integrity["blocking"])
            self.assertIn(
                "generator_cross_group_pair",
                {row["code"] for row in integrity["integrity_issues"]},
            )
            self.assertEqual(
                next(
                    row for row in integrity["managed_meshes"]
                    if row["mesh_id"] == 2
                )["classification"],
                "ambiguous",
            )

    def test_managed_generator_reference_without_guid_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_missing_guid.spm"
            model = ET.Element("SpeedTreeModel")
            assets = ET.SubElement(model, "Assets")
            add_material(assets, 10, "M_leaf_current", [1], "scope-current")
            add_external_mesh(assets, root, 1, "scope-current")
            add_generator(model, 10, 1, guid=False)
            write_spm(spm, model)
            write_receipt(
                spm,
                ".atlas_leaf_speedtree_targets",
                "SK_missing_guid.json",
                "scope-current",
                10,
                "M_leaf_current",
                [1],
            )

            integrity = inspect_spm_mesh_file_references(spm)[
                "atlas_consumer_integrity"
            ]

            self.assertTrue(integrity["blocking"])
            self.assertIn(
                "generator_guid_missing",
                {row["code"] for row in integrity["integrity_issues"]},
            )


if __name__ == "__main__":
    unittest.main()
