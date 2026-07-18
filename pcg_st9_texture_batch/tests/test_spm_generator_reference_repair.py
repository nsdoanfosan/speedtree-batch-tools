import gzip
import hashlib
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import spm_generator_reference_repair as repair


def builder_marker(scope, kind, group=""):
    payload = {
        "generator": repair.ATLAS_LEAF_GENERATOR,
        "scope": scope,
        "kind": kind,
    }
    if group:
        payload["group"] = group
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def add_material(assets, material_id, name, mesh_ids, user_data=""):
    material = ET.SubElement(
        assets, "Material_v8", {"ID": str(material_id), "Name": name})
    ET.SubElement(material, "CutoutMeshID").text = str(mesh_ids[0])
    supplemental = ET.SubElement(
        material,
        "SupplementalCutoutMeshIDs",
        {"Count": str(max(0, len(mesh_ids) - 1))},
    )
    for mesh_id in mesh_ids[1:]:
        ET.SubElement(supplemental, "CutoutMesh", {"ID": str(mesh_id)})
    ET.SubElement(material, "UserData").text = user_data
    return material


def add_mesh(assets, mesh_id, name, filename="", user_data="", embedded=True):
    mesh = ET.SubElement(assets, "Mesh", {"ID": str(mesh_id), "Name": name})
    ET.SubElement(mesh, "Filename").text = filename
    ET.SubElement(mesh, "Embedded").text = "true" if embedded else "false"
    ET.SubElement(mesh, "UserData").text = user_data
    return mesh


def add_generator(root, generator_type, name, slots, guid=None):
    generator = ET.SubElement(root, "Generator", {"Type": generator_type})
    ET.SubElement(generator, "Name").text = name
    ET.SubElement(generator, "GUID").text = guid or f"guid-{name.lower()}"
    properties = ET.SubElement(generator, "Properties")
    for index, (material_id, mesh_id) in enumerate(slots):
        prefix = f"Leaves:Type:{index}"
        for suffix, value in (("Material", material_id), ("Mesh", mesh_id)):
            prop = ET.SubElement(properties, "Property")
            ET.SubElement(prop, "Name").text = f"{prefix}:{suffix}"
            ET.SubElement(prop, "Value").text = str(value)
    return generator


def write_spm(path, root):
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    Path(path).write_bytes(gzip.compress(xml, mtime=0))


def spm_root(path):
    return ET.fromstring(gzip.decompress(Path(path).read_bytes()))


def generator_pairs(path):
    result = {}
    root = spm_root(path)
    for generator_index, generator in enumerate(root.iter("Generator")):
        properties = generator.find("Properties")
        if properties is None:
            continue
        values = {}
        for prop in properties.findall("Property"):
            values[prop.findtext("Name")] = int(prop.findtext("Value"))
        for name, material_id in values.items():
            if not name.endswith(":Material"):
                continue
            prefix = name[:-len(":Material")]
            result[(generator_index, prefix)] = (
                material_id, values[prefix + ":Mesh"])
    return result


class GeneratorReferenceRepairTests(unittest.TestCase):
    def make_spm(
            self, root, filename="SK_test.spm", duplicate_ordinal=False,
            omit_managed_mesh=False):
        root = Path(root)
        meshes_dir = root / "meshes"
        meshes_dir.mkdir(parents=True, exist_ok=True)
        model = ET.Element("SpeedTreeModel")
        assets = ET.SubElement(model, "Assets")

        add_material(assets, 4, "M_leaf_source", [6, 7])
        add_mesh(assets, 6, "source_leaf_01")
        add_mesh(assets, 7, "source_leaf_02")

        scope = "scope-test"
        add_material(
            assets,
            10,
            "M_leaf_test_atlas_01_stem",
            [20],
            builder_marker(scope, "material", "stem"),
        )
        add_material(
            assets,
            11,
            "M_leaf_test_atlas_01_green",
            [21],
            builder_marker(scope, "material", "green"),
        )
        mesh_20_name = "stem__01_leaf_01_front_01"
        mesh_21_name = (
            "green__01_leaf_01_front_02"
            if duplicate_ordinal else "green__02_leaf_02_front_02"
        )
        for mesh_id, mesh_name, group in (
                (20, mesh_20_name, "stem"),
                (21, mesh_21_name, "green")):
            if omit_managed_mesh and mesh_id == 21:
                continue
            relative = f"meshes/{mesh_name}.fbx"
            (root / relative).write_bytes(f"fbx-{mesh_id}".encode())
            add_mesh(
                assets,
                mesh_id,
                mesh_name,
                relative,
                builder_marker(scope, "mesh", group),
                embedded=False,
            )

        add_generator(model, "Leaf Mesh", "Leaf", [(4, 6), (4, 7)])
        add_generator(
            model, "Frond", "Frond", [(11, -10)], guid="guid-frond")
        path = root / filename
        write_spm(path, model)
        source_model = ET.Element("SpeedTreeModel")
        source_assets = ET.SubElement(source_model, "Assets")
        add_material(source_assets, 4, "M_leaf_source", [6, 7])
        add_mesh(source_assets, 6, "source_leaf_01")
        add_mesh(source_assets, 7, "source_leaf_02")
        add_generator(
            source_model, "Leaf Mesh", "Leaf", [(4, 6), (4, 7)],
            guid="guid-leaf")
        add_generator(
            source_model, "Frond", "Frond", [(4, 7)],
            guid="guid-frond")
        source_path = root / ("source_" + filename)
        write_spm(source_path, source_model)
        if not hasattr(self, "_authoritative_sources"):
            self._authoritative_sources = {}
        self._authoritative_sources[path] = source_path
        return path

    def source_set(self, spm):
        return {
            "atlas_base": "M_leaf_test_atlas_01",
            "source_material_ids": [4],
            "source_material_names": ["M_leaf_source"],
            "managed_material_ids": [10, 11],
            "managed_material_names": [
                "M_leaf_test_atlas_01_stem",
                "M_leaf_test_atlas_01_green",
            ],
            "authoritative_source_spm": str(
                self._authoritative_sources[Path(spm)]),
            "authoritative_source_material_ids": [4],
            "authoritative_source_material_names": ["M_leaf_source"],
        }

    def test_dry_run_and_apply_change_only_generator_references(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = self.make_spm(root)
            original_bytes = spm.read_bytes()
            original_assets = ET.tostring(spm_root(spm).find("Assets"))
            fbx_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (root / "meshes").glob("*.fbx")
            }

            plan = repair.build_repair_plan(spm, self.source_set(spm))

            self.assertEqual(spm.read_bytes(), original_bytes)
            self.assertEqual(plan["original_sha256"], hashlib.sha256(original_bytes).hexdigest())
            self.assertEqual(plan["change_count"], 3)
            self.assertEqual(
                [row["reason"] for row in plan["changes"]],
                [
                    "authoritative_source_to_managed",
                    "authoritative_source_to_managed",
                    "authoritative_managed_mesh_sentinel",
                ],
            )
            self.assertEqual(plan["changes"][0]["before"], {
                "material_id": 4, "mesh_id": 6})
            self.assertEqual(plan["changes"][0]["after"]["material_id"], 10)
            self.assertEqual(plan["changes"][0]["after"]["mesh_id"], 20)

            result = repair.apply_repair_plans(plan)

            self.assertEqual(result["status"], "applied")
            self.assertEqual(generator_pairs(spm), {
                (0, "Leaves:Type:0"): (10, 20),
                (0, "Leaves:Type:1"): (11, 21),
                (1, "Leaves:Type:0"): (11, 21),
            })
            self.assertEqual(ET.tostring(spm_root(spm).find("Assets")), original_assets)
            self.assertEqual({
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in fbx_hashes
            }, fbx_hashes)
            manifest = json.loads(
                Path(result["manifests"][0]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "applied")
            self.assertTrue(Path(manifest["backup"]).is_file())
            self.assertEqual(
                hashlib.sha256(Path(manifest["backup"]).read_bytes()).hexdigest(),
                plan["original_sha256"],
            )

            second_plan = repair.build_repair_plan(spm, self.source_set(spm))
            self.assertEqual(second_plan["changes"], [])
            after_first_apply = spm.read_bytes()
            second_result = repair.apply_repair_plans(second_plan)
            self.assertEqual(second_result["status"], "unchanged")
            self.assertEqual(spm.read_bytes(), after_first_apply)

    def test_duplicate_managed_leaf_ordinal_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = self.make_spm(temp, duplicate_ordinal=True)
            with self.assertRaisesRegex(
                    repair.ReferenceRepairError, "duplicate leaf ordinal 1"):
                repair.build_repair_plan(spm, self.source_set(spm))

    def test_incomplete_managed_output_can_restore_exact_source_pairs(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = self.make_spm(temp)
            root = spm_root(spm)
            for generator in root.iter("Generator"):
                for prop in generator.find("Properties").findall("Property"):
                    name = prop.findtext("Name") or ""
                    if name.endswith(":Material"):
                        prop.find("Value").text = "10"
            write_spm(spm, root)
            source_set = self.source_set(spm)
            source_set.update({
                "output_policy": "restore_source",
                "managed_material_ids": [10],
                "managed_material_names": [
                    "M_leaf_test_atlas_01_stem"],
            })

            plan = repair.build_repair_plan(spm, source_set)

            self.assertEqual(plan["change_count"], 3)
            self.assertEqual(plan["restored_source_slots"], 3)
            self.assertEqual(
                {row["reason"] for row in plan["changes"]},
                {"authoritative_managed_to_source_restore"},
            )
            self.assertTrue(all(
                row["output_policy"] == "restore_source"
                for row in plan["changes"]
            ))
            repair.apply_repair_plans(plan)
            self.assertEqual(generator_pairs(spm), {
                (0, "Leaves:Type:0"): (4, 6),
                (0, "Leaves:Type:1"): (4, 7),
                (1, "Leaves:Type:0"): (4, 7),
            })

    def test_unrelated_duplicate_cutout_does_not_block_reference_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = self.make_spm(temp)
            root = spm_root(spm)
            assets = root.find("Assets")
            add_mesh(assets, 30, "unrelated")
            add_material(assets, 99, "M_unrelated", [30, 30])
            write_spm(spm, root)

            plan = repair.build_repair_plan(spm, self.source_set(spm))

            self.assertEqual(plan["change_count"], 3)

    def test_authoritative_random_sentinel_is_not_coerced_to_ordinal_one(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = self.make_spm(temp)
            source = self._authoritative_sources[spm]
            root = spm_root(source)
            frond = list(root.iter("Generator"))[1]
            values = {
                prop.findtext("Name"): prop.find("Value")
                for prop in frond.find("Properties").findall("Property")
            }
            values["Leaves:Type:0:Mesh"].text = "-10"
            write_spm(source, root)

            plan = repair.build_repair_plan(spm, self.source_set(spm))

            self.assertEqual(plan["change_count"], 2)
            self.assertEqual(plan["authoritative_sentinel_slots"], 1)
            self.assertFalse(any(
                row["generator_guid"] == "guid-frond"
                for row in plan["changes"]
            ))

    def test_restore_source_rejects_changed_mesh_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = self.make_spm(temp)
            root = spm_root(spm)
            mesh = next(
                row for row in root.find("Assets").findall("Mesh")
                if row.get("ID") == "6")
            mesh.set("Name", "unexpected_mesh")
            write_spm(spm, root)
            source_set = self.source_set(spm)
            source_set["output_policy"] = "restore_source"

            with self.assertRaisesRegex(
                    repair.ReferenceRepairError,
                    "restore_source Mesh identity differs"):
                repair.build_repair_plan(spm, source_set)

    def test_missing_managed_mesh_asset_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = self.make_spm(temp, omit_managed_mesh=True)
            with self.assertRaisesRegex(
                    repair.ReferenceRepairError, "references missing Mesh 21"):
                repair.build_repair_plan(spm, self.source_set(spm))

    def test_missing_managed_fbx_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = self.make_spm(root)
            next((root / "meshes").glob("green*.fbx")).unlink()
            with self.assertRaisesRegex(
                    repair.ReferenceRepairError, "FBX is missing"):
                repair.build_repair_plan(spm, self.source_set(spm))

    def test_authoritative_source_repairs_wrong_positive_and_missing_mesh(self):
        with tempfile.TemporaryDirectory() as temp:
            spm = self.make_spm(temp)
            root = spm_root(spm)
            generators = list(root.iter("Generator"))
            first_values = {
                prop.findtext("Name"): prop.find("Value")
                for prop in generators[0].find("Properties").findall("Property")
            }
            first_values["Leaves:Type:0:Material"].text = "10"
            first_values["Leaves:Type:0:Mesh"].text = "21"
            first_values["Leaves:Type:1:Material"].text = "11"
            first_values["Leaves:Type:1:Mesh"].text = "999"
            write_spm(spm, root)

            plan = repair.build_repair_plan(spm, self.source_set(spm))

            reasons = {row["reason"] for row in plan["changes"]}
            self.assertIn("authoritative_material_mesh_mismatch", reasons)
            self.assertIn("authoritative_missing_mesh_reference", reasons)
            repair.apply_repair_plans(plan)
            self.assertEqual(generator_pairs(spm), {
                (0, "Leaves:Type:0"): (10, 20),
                (0, "Leaves:Type:1"): (11, 21),
                (1, "Leaves:Type:0"): (11, 21),
            })

    def test_parsley_style_missing_pair_uses_authoritative_guid_and_ordinal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_model = ET.Element("SpeedTreeModel")
            target_assets = ET.SubElement(target_model, "Assets")
            add_material(target_assets, 4, "M_leaf_parsley_02", [6, 7, 8, 9, 10])
            for mesh_id in (6, 7, 8, 9, 10):
                add_mesh(target_assets, mesh_id, f"source_{mesh_id}")
            scope = "parsley-scope"
            for material_id, name, mesh_ids, group in (
                (7, "M_leaf_parsley_atlas_02_stem", [63, 64], "Stem"),
                (8, "M_leaf_parsley_atlas_02_green", [71], "Green"),
                (9, "M_leaf_parsley_atlas_02_yellow", [78], "Yellow"),
            ):
                add_material(
                    target_assets, material_id, name, mesh_ids,
                    builder_marker(scope, "material", group))
            for mesh_id, ordinal, group in (
                (63, 1, "Stem"), (64, 2, "Stem"),
                (71, 4, "Green"), (78, 5, "Yellow"),
            ):
                add_mesh(
                    target_assets, mesh_id,
                    f"parsley__{ordinal:02d}_leaf_{ordinal:02d}_front",
                    user_data=builder_marker(scope, "mesh", group))

            target_generators = (
                ("g0", [(8, -10)]),
                ("g1", [(8, -10)]),
                ("g5", [(7, 63), (7, 64)]),
                ("g6", [(8, 71), (8, -10)]),
                ("g7", [(7, -10)]),
            )
            for guid, slots in target_generators:
                add_generator(
                    target_model, "Leaf Mesh", guid, slots, guid=guid)
            target = root / "SK_weed_parsley_01.spm"
            write_spm(target, target_model)

            source_model = ET.Element("SpeedTreeModel")
            source_assets = ET.SubElement(source_model, "Assets")
            add_material(source_assets, 4, "leaf_parsley_02", [6, 7, 8, 9, 10])
            for mesh_id in (6, 7, 8, 9, 10):
                add_mesh(source_assets, mesh_id, f"source_{mesh_id}")
            source_generators = (
                ("g0", [(4, 9)]),
                ("g1", [(4, 6), (4, 7)]),
                ("g5", [(4, 6), (4, 7)]),
                ("g6", [(4, 9), (4, 9)]),
                ("g7", [(4, 10)]),
            )
            for guid, slots in source_generators:
                add_generator(
                    source_model, "Leaf Mesh", guid, slots, guid=guid)
            source = root / "weed_parsley_01.spm"
            write_spm(source, source_model)
            source_set = {
                "atlas_base": "M_leaf_parsley_atlas_02",
                "source_material_ids": [4],
                "source_material_names": ["M_leaf_parsley_02"],
                "managed_material_ids": [7, 8, 9],
                "managed_material_names": [
                    "M_leaf_parsley_atlas_02_stem",
                    "M_leaf_parsley_atlas_02_green",
                    "M_leaf_parsley_atlas_02_yellow",
                ],
                "authoritative_source_spm": str(source),
                "authoritative_source_material_ids": [4],
                "authoritative_source_material_names": ["leaf_parsley_02"],
            }

            plan = repair.build_repair_plan(target, source_set)

            self.assertEqual(plan["expected_slot_count"], 8)
            self.assertEqual(plan["change_count"], 5)
            self.assertEqual(plan["inserted_slot_pair_count"], 1)
            inserted = [row for row in plan["changes"]
                        if row["operation"] == "insert_pair"]
            self.assertEqual(inserted[0]["generator_guid"], "g1")
            self.assertEqual(inserted[0]["ordinal"], 2)
            repair.apply_repair_plans(plan)
            self.assertEqual(generator_pairs(target), {
                (0, "Leaves:Type:0"): (8, 71),
                (1, "Leaves:Type:0"): (7, 63),
                (1, "Leaves:Type:1"): (7, 64),
                (2, "Leaves:Type:0"): (7, 63),
                (2, "Leaves:Type:1"): (7, 64),
                (3, "Leaves:Type:0"): (8, 71),
                (3, "Leaves:Type:1"): (8, 71),
                (4, "Leaves:Type:0"): (9, 78),
            })

    def test_hash_guard_rejects_changed_spm_before_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spm = self.make_spm(root)
            plan = repair.build_repair_plan(spm, self.source_set(spm))
            xml = gzip.decompress(spm.read_bytes()).replace(
                b"</SpeedTreeModel>", b"<!-- changed --></SpeedTreeModel>")
            spm.write_bytes(gzip.compress(xml, mtime=0))
            changed_hash = repair.sha256_file(spm)

            with self.assertRaisesRegex(
                    repair.ReferenceRepairError, "hash guard failed"):
                repair.apply_repair_plans(plan)

            self.assertEqual(repair.sha256_file(spm), changed_hash)
            self.assertFalse((root / "_spm_backups").exists())

    def test_validate_plans_exercises_patch_without_changing_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self.make_spm(root, "SK_first.spm")
            second = self.make_spm(root, "SK_second.spm")
            plans = [
                repair.build_repair_plan(first, self.source_set(first)),
                repair.build_repair_plan(second, self.source_set(second)),
            ]
            original_bytes = {
                first: first.read_bytes(),
                second: second.read_bytes(),
            }

            result = repair.validate_repair_plans(plans)

            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["change_count"], 6)
            self.assertEqual(
                [row["status"] for row in result["results"]],
                ["validated", "validated"],
            )
            self.assertEqual({
                path: path.read_bytes() for path in original_bytes
            }, original_bytes)
            self.assertFalse((root / "_spm_backups").exists())
            self.assertEqual(
                list(root.glob(".*.reference_repair.*.spm")), [])

    def test_multi_spm_commit_failure_rolls_back_every_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self.make_spm(root, "SK_first.spm")
            second = self.make_spm(root, "SK_second.spm")
            plans = [
                repair.build_repair_plan(first, self.source_set(first)),
                repair.build_repair_plan(second, self.source_set(second)),
            ]
            original_hashes = {
                first: repair.sha256_file(first),
                second: repair.sha256_file(second),
            }
            original_commit = repair._commit_spm
            calls = []

            def fail_second_commit(temp_path, target_path):
                calls.append(Path(target_path))
                if len(calls) == 2:
                    raise OSError("forced second commit failure")
                return original_commit(temp_path, target_path)

            with mock.patch.object(
                    repair, "_commit_spm", side_effect=fail_second_commit):
                with self.assertRaisesRegex(
                        repair.ReferenceRepairError,
                        "forced second commit failure"):
                    repair.apply_repair_plans(plans)

            self.assertEqual({
                path: repair.sha256_file(path) for path in original_hashes
            }, original_hashes)
            manifests = list((root / "_spm_backups").rglob(
                "*.generator_reference_repair.json"))
            self.assertEqual(len(manifests), 2)
            self.assertTrue(all(
                json.loads(path.read_text(encoding="utf-8"))["status"]
                == "rolled_back"
                for path in manifests
            ))


if __name__ == "__main__":
    unittest.main()
