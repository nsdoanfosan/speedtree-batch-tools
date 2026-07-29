import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from speedtree_texture_contract import (
    REQUIRED_TEXTURE_ROLES,
    build_spm_canonical_texture_plan,
    index_texture_sets,
    inspect_production_spm_texture_contract,
    inspect_spm_texture_slots,
    normalize_material_key,
    normalize_texture_set_key,
    parse_managed_texture_path,
    read_stmat_material_sources,
    rebase_spm_copy_to_canonical_outputs,
    resolve_texture_bindings,
    resolve_texture_set,
)


class SpeedTreeTextureContractTests(unittest.TestCase):
    def _write_set(self, directory, base, roles=REQUIRED_TEXTURE_ROLES):
        directory.mkdir(parents=True, exist_ok=True)
        for role in roles:
            (directory / f"{base}_{role}.png").write_bytes(role.encode("ascii"))

    def _write_stmat(self, path, materials):
        rows = []
        for material_name, sources in materials:
            maps = "".join(
                f'<Map Name="{map_name}" Source="{source}" />'
                for map_name, source in sources
            )
            rows.append(f'<Material Name="{material_name}">{maps}</Material>')
        path.write_text(
            "<SpeedTreeMaterials>" + "".join(rows) + "</SpeedTreeMaterials>",
            encoding="utf-8",
        )

    def _write_spm(self, path, material_name, refs):
        maps = "".join(
            (
                f'<Map Name="{map_name}"><TexFilename>{ref}</TexFilename>'
                "</Map>"
            )
            for map_name, ref in refs
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '<SpeedTree><Materials><Material_v8 ID="7" '
            f'Name="{material_name}">{maps}</Material_v8>'
            "</Materials></SpeedTree>",
            encoding="utf-8",
        )

    def _write_manifest(
        self,
        root,
        spm,
        *,
        material_name="M_leaf_test",
        texture_base="T_leaf_test",
        omit_role=None,
    ):
        texture_root = root / "texture"
        texture_root.mkdir(parents=True, exist_ok=True)
        files = {}
        for role in REQUIRED_TEXTURE_ROLES:
            name = f"{texture_base}_{role}.tga"
            files[role] = name
            if role != omit_role:
                (texture_root / name).write_bytes(role.encode("ascii"))
        manifest = texture_root / "pcg_st9_canonical_outputs.json"
        manifest.write_text(
            json.dumps({
                "kind": "pcg_st9_canonical_output_manifest",
                "schema_version": 1,
                "asset_root": str(root.resolve()),
                "texture_root": str(texture_root.resolve()),
                "outputs": [{
                    "texture_base": texture_base,
                    "required_roles": list(REQUIRED_TEXTURE_ROLES),
                    "files": files,
                    "material_targets": [{
                        "spm": str(spm.resolve()),
                        "material_id": "7",
                        "material_name": material_name,
                    }],
                    "producer": {
                        "tool": "PCG ST9 Texture",
                        "source": "unit-test",
                    },
                }],
            }),
            encoding="utf-8",
        )
        return manifest, texture_root

    def _write_blender_bake_receipt(
        self,
        asset_root,
        spm,
        material_id,
        material_name,
        slot_files,
        *,
        role_override=None,
    ):
        capture_root = asset_root / "cluster"
        capture_root.mkdir(parents=True, exist_ok=True)
        contract_hash = "a" * 64
        maps = []
        for row in slot_files:
            path = Path(row["path"]).resolve()
            maps.append({
                "role": (
                    role_override
                    if row["map"] == "AO" and role_override
                    else row["map"]
                ),
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            })
        manifest = capture_root / "branch_test_01_auto_capture_manifest.json"
        manifest.write_text(
            json.dumps({
                "kind": "speedtree_cluster_blender_auto_capture",
                "version": 2,
                "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
                "direct_uv_source":
                    "same_blender_physical_capture_projection",
                "physical_capture_contract_sha256": contract_hash,
                "maps": maps,
            }),
            encoding="utf-8",
        )
        return {
            "kind": "blender_cluster_bake_texture_origin_receipt",
            "version": 1,
            "source_origin": "blender_cluster_bake",
            "physical_capture_manifest": str(manifest.resolve()),
            "physical_capture_contract_sha256": contract_hash,
            "source_spm": str(spm.resolve()),
            "material_id": material_id,
            "material_name": material_name,
            "source_refs": [
                str(Path(row["path"]).resolve())
                for row in slot_files
            ],
        }

    def test_name_and_managed_path_normalization(self):
        self.assertEqual(normalize_material_key("Green-Mat.001"), "greenmat")
        self.assertEqual(normalize_material_key("Green_Mat.001"), "green")
        self.assertEqual(
            normalize_texture_set_key("M_Weed Common 01"), "weedcommon01"
        )
        self.assertEqual(
            normalize_texture_set_key("T_Weed_Common_01_Normal.png"),
            "weedcommon01",
        )
        parsed = parse_managed_texture_path(
            r"D:\textures\T_Weed_Common_01_Subsurface.TGA"
        )
        self.assertEqual(parsed["texture_base"], "T_Weed_Common_01")
        self.assertEqual(parsed["role"], "subsurface")
        self.assertIsNone(parse_managed_texture_path("weed_normal.png"))

    def test_two_material_slots_can_share_one_complete_referenced_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            textures = root / "texture"
            self._write_set(textures, "T_Weed_Common_01")
            stmat = root / "plant.stmat"
            shared_sources = [
                ("Color", "texture/T_Weed_Common_01_Color.png"),
                ("Normal", "texture/T_Weed_Common_01_Normal.png"),
                ("Extra", "texture/T_Weed_Common_01_Extra.png"),
                ("Height", "texture/T_Weed_Common_01_Height.png"),
            ]
            self._write_stmat(
                stmat,
                [("Green", shared_sources), ("Dead", shared_sources)],
            )

            result = resolve_texture_bindings(stmat)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["bindings"]), 2)
            self.assertEqual(
                [row["material"] for row in result["bindings"]],
                ["Green", "Dead"],
            )
            self.assertEqual(
                {row["set_key"] for row in result["bindings"]},
                {"weedcommon01"},
            )
            self.assertEqual(
                set(result["bindings"][0]["files"]), set(REQUIRED_TEXTURE_ROLES)
            )
            self.assertEqual(result["missing"], [])
            json.dumps(result, sort_keys=True)

    def test_case_variant_role_file_stays_in_one_set(self):
        # Regression: T_Leaf_velvet_grass_Atlas_02_* plus a stray lower-case
        # T_leaf_velvet_grass_atlas_02_extra.tga must resolve as one complete
        # set with the majority spelling, not as ambiguous bases.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            textures = root / "texture"
            self._write_set(
                textures,
                "T_Leaf_velvet_grass_Atlas_02",
                roles=("color", "normal", "height", "opacity", "subsurface"),
            )
            (textures / "T_leaf_velvet_grass_atlas_02_extra.png").write_bytes(
                b"extra"
            )
            stmat = root / "plant.stmat"
            self._write_stmat(
                stmat,
                [
                    (
                        "M_Leaf_velvet_grass_Atlas_02_Mat",
                        [
                            (
                                "Color",
                                "texture/T_leaf_velvet_grass_atlas_02_color.png",
                            ),
                            (
                                "Normal",
                                "texture/T_leaf_velvet_grass_atlas_02_normal.png",
                            ),
                        ],
                    )
                ],
            )

            indexed = index_texture_sets(textures)["leafvelvetgrassatlas02"]
            result = resolve_texture_bindings(stmat)

            self.assertEqual(len(indexed), 1)
            self.assertFalse(indexed[0]["ambiguous_bases"])
            self.assertTrue(indexed[0]["complete"])
            self.assertEqual(
                indexed[0]["texture_bases"], ["T_Leaf_velvet_grass_Atlas_02"]
            )
            self.assertEqual(result["status"], "ok")
            binding = result["bindings"][0]
            self.assertEqual(binding["status"], "ok")
            self.assertEqual(
                binding["texture_base"], "T_Leaf_velvet_grass_Atlas_02"
            )
            self.assertEqual(set(binding["files"]), set(REQUIRED_TEXTURE_ROLES))
            self.assertEqual(result["missing"], [])

    def test_complete_candidate_wins_over_earlier_partial_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "a_partial"
            complete = root / "z_complete"
            self._write_set(
                partial,
                "T_Shared_01",
                roles=("color", "normal", "extra", "height"),
            )
            self._write_set(complete, "T_Shared_01")
            stmat = root / "plant.stmat"
            self._write_stmat(
                stmat,
                [
                    (
                        "Yellow",
                        [
                            ("Color", "a_partial/T_Shared_01_Color.png"),
                            ("Normal", "a_partial/T_Shared_01_Normal.png"),
                        ],
                    )
                ],
            )

            result = resolve_texture_bindings(stmat, [partial, complete])

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                Path(result["bindings"][0]["texture_dir"]), complete.resolve()
            )
            candidates = index_texture_sets([partial, complete])["shared01"]
            self.assertEqual([row["complete"] for row in candidates], [False, True])

    def test_single_base_resolver_uses_complete_set_and_rejects_material_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "a_partial"
            complete = root / "z_complete"
            self._write_set(partial, "T_Shared_01", roles=("color", "normal"))
            self._write_set(complete, "T_Shared_01")

            result = resolve_texture_set(
                [partial, complete], "T_Shared_01", REQUIRED_TEXTURE_ROLES
            )
            material_name = resolve_texture_set(
                [partial, complete], "Green", REQUIRED_TEXTURE_ROLES
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(Path(result["texture_dir"]), complete.resolve())
            self.assertEqual(list(result["files"]), list(REQUIRED_TEXTURE_ROLES))
            self.assertEqual(result["missing_roles"], [])
            self.assertEqual(material_name["status"], "invalid_texture_base")
            self.assertEqual(material_name["files"], {})
            self.assertEqual(
                material_name["missing_roles"], list(REQUIRED_TEXTURE_ROLES)
            )

    def test_missing_roles_are_reported_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            textures = root / "texture"
            self._write_set(textures, "T_Stem_01", roles=("color", "normal"))
            stmat = root / "plant.stmat"
            self._write_stmat(
                stmat,
                [
                    (
                        "Stem",
                        [
                            ("Color", "texture/T_Stem_01_Color.png"),
                            ("Normal", "texture/T_Stem_01_Normal.png"),
                        ],
                    )
                ],
            )

            first = resolve_texture_bindings(stmat)
            second = resolve_texture_bindings(stmat)

            expected = ["extra", "height", "opacity", "subsurface"]
            self.assertEqual(first["status"], "incomplete")
            self.assertEqual(first["missing"][0]["missing_roles"], expected)
            self.assertEqual(first, second)
            self.assertEqual(
                json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
            )

    def test_read_stmat_preserves_duplicate_named_material_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stmat = root / "plant.stmat"
            self._write_stmat(
                stmat,
                [
                    ("Green", [("Color", "T_Set_Color.png")]),
                    ("Green", [("Normal", "T_Set_Normal.png")]),
                ],
            )

            parsed = read_stmat_material_sources(stmat)

            self.assertEqual(parsed["status"], "ok")
            self.assertEqual(len(parsed["materials"]), 2)
            self.assertEqual(
                [row["material_index"] for row in parsed["materials"]], [0, 1]
            )
            self.assertEqual(
                [row["sources"][0]["map"] for row in parsed["materials"]],
                ["Color", "Normal"],
            )

    def test_non_managed_speedtree_sources_remain_legacy_not_applicable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cluster" / "leaf_color.tga"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"legacy cluster")
            stmat = root / "plant.stmat"
            self._write_stmat(
                stmat,
                [("Cluster", [("Color", source.as_posix())])],
            )

            result = resolve_texture_bindings(stmat)

            self.assertEqual(result["status"], "not_applicable")
            self.assertEqual(result["managed_material_count"], 0)
            self.assertEqual(result["bindings"][0]["status"], "not_managed")
            self.assertEqual(result["missing"], [])

    def test_production_spm_rejects_original_and_rebases_isolated_to_t_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_test"
            source = root / "Cluster" / "SK_leaf_test.spm"
            original = root / "authoring" / "leaf_albedo.tif"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"original")
            self._write_spm(
                source,
                "M_leaf_test",
                [
                    ("Color", str(original)),
                    ("Normal", str(root / "authoring" / "leaf_normal.tif")),
                ],
            )
            (root / "authoring" / "leaf_normal.tif").write_bytes(b"normal")
            manifest, texture_root = self._write_manifest(root, source)

            production = inspect_production_spm_texture_contract(
                source, manifest
            )

            self.assertEqual(production["status"], "blocked")
            self.assertEqual(
                {
                    (row["material"], row["role"], row["reason"])
                    for row in production["issues"]
                },
                {
                    (
                        "M_leaf_test",
                        "color",
                        "production_texture_not_canonical_output",
                    ),
                    (
                        "M_leaf_test",
                        "normal",
                        "production_texture_not_canonical_output",
                    ),
                },
            )
            self.assertTrue(all(
                "PCG ST9 Texture" in row["remediation"]
                for row in production["issues"]
            ))

            isolated = (
                root / ".sk_batch_isolated_bark" / "hash" / source.name
            )
            isolated.parent.mkdir(parents=True)
            isolated.write_bytes(source.read_bytes())
            plan = build_spm_canonical_texture_plan(source, manifest)
            result = rebase_spm_copy_to_canonical_outputs(
                isolated, source, plan
            )
            slots = inspect_spm_texture_slots(isolated)["materials"][0]["slots"]

            self.assertEqual(result["rewritten_reference_count"], 2)
            self.assertEqual(
                {
                    Path(row["resolved_ref"]).resolve()
                    for row in slots
                },
                {
                    (texture_root / "T_leaf_test_color.tga").resolve(),
                    (texture_root / "T_leaf_test_normal.tga").resolve(),
                },
            )
            self.assertEqual(original.read_bytes(), b"original")

    def test_missing_manifest_role_reports_material_role_and_expected_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_test"
            source = root / "Cluster" / "SK_leaf_test.spm"
            self._write_spm(
                source,
                "M_leaf_test",
                [("Color", "old_color.tif")],
            )
            manifest, texture_root = self._write_manifest(
                root, source, omit_role="normal"
            )

            plan = build_spm_canonical_texture_plan(source, manifest)

            self.assertEqual(plan["status"], "blocked")
            missing = next(
                row for row in plan["issues"]
                if row["reason"] == "canonical_output_missing"
            )
            self.assertEqual(missing["material"], "M_leaf_test")
            self.assertEqual(missing["material_id"], "7")
            self.assertEqual(missing["role"], "normal")
            self.assertEqual(
                Path(missing["expected_output"]),
                texture_root / "T_leaf_test_normal.tga",
            )
            self.assertIn("PCG ST9 Texture", missing["remediation"])

    def test_canonical_manifest_requires_the_exact_six_role_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_test"
            source = root / "Cluster" / "SK_leaf_test.spm"
            self._write_spm(
                source,
                "M_leaf_test",
                [("Color", "old_color.tif")],
            )
            manifest, _texture_root = self._write_manifest(root, source)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["outputs"][0]["required_roles"] = [
                role
                for role in REQUIRED_TEXTURE_ROLES
                if role != "subsurface"
            ]
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            plan = build_spm_canonical_texture_plan(source, manifest)

            self.assertEqual(plan["status"], "blocked")
            issue = next(
                row for row in plan["issues"]
                if row["reason"] == "invalid_required_roles"
            )
            self.assertEqual(issue["material"], "M_leaf_test")
            self.assertEqual(
                set(issue["required_roles"]),
                set(REQUIRED_TEXTURE_ROLES) - {"subsurface"},
            )

    def test_manifest_lookup_does_not_escape_the_asset_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            forest = Path(temporary) / "Forest"
            asset = forest / "Tree_test"
            source = asset / "Cluster" / "SK_leaf_test.spm"
            self._write_spm(
                source,
                "M_leaf_test",
                [("Color", "old_color.tif")],
            )
            parent_manifest, _texture_root = self._write_manifest(
                forest,
                source,
            )
            self.assertTrue(parent_manifest.is_file())

            plan = build_spm_canonical_texture_plan(source)

            self.assertEqual(plan["status"], "blocked")
            issue = next(
                row for row in plan["issues"]
                if row["reason"] == "canonical_output_manifest_missing"
            )
            self.assertEqual(
                Path(issue["expected_output"]),
                asset / "texture" / "pcg_st9_canonical_outputs.json",
            )

    def test_shared_owner_manifest_allows_exact_external_material_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            forest = Path(temporary) / "Forest"
            owner = forest / "Tree_owner"
            consumer = forest / "Tree_consumer"
            source = consumer / "Cluster" / "SK_leaf_test.spm"
            self._write_spm(
                source,
                "M_leaf_test",
                [("Color", "old_color.tif")],
            )
            manifest, texture_root = self._write_manifest(owner, source)

            plan = build_spm_canonical_texture_plan(source, manifest)

            self.assertEqual(plan["status"], "ok")
            self.assertEqual(Path(plan["asset_root"]), owner.resolve())
            self.assertEqual(Path(plan["texture_root"]), texture_root.resolve())
            self.assertEqual(plan["bindings"][0]["material_id"], "7")
            self.assertEqual(
                plan["bindings"][0]["origin_state"],
                "canonical_t",
            )

    def test_shared_owner_manifest_rejects_undeclared_external_spm(self):
        with tempfile.TemporaryDirectory() as temporary:
            forest = Path(temporary) / "Forest"
            owner = forest / "Tree_owner"
            consumer = forest / "Tree_consumer"
            source = consumer / "Cluster" / "SK_leaf_test.spm"
            self._write_spm(
                source,
                "M_leaf_test",
                [("Color", "old_color.tif")],
            )
            other = owner / "SK_other.spm"
            manifest, _texture_root = self._write_manifest(owner, other)

            plan = build_spm_canonical_texture_plan(source, manifest)

            self.assertEqual(plan["status"], "blocked")
            self.assertIn(
                "production_spm_outside_manifest_asset",
                {row["reason"] for row in plan["issues"]},
            )

    def test_blender_cluster_bake_preserves_all_eight_speedtree_map_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_test"
            production = root / "SK_tree_test_01.spm"
            map_names = (
                "Color",
                "Opacity",
                "Normal",
                "Gloss",
                "SubsurfaceColor",
                "SubsurfaceAmount",
                "AO",
                "Height",
            )
            self._write_spm(
                production,
                "M_branch_test_01",
                [
                    (map_name, f"old/{map_name}.png")
                    for map_name in map_names
                ],
            )
            bake_root = root / "cluster"
            bake_root.mkdir(parents=True, exist_ok=True)
            slot_files = []
            expected_by_map = {}
            for map_index, map_name in enumerate(map_names):
                output = bake_root / f"branch_test_01_{map_name}.tga"
                output.write_bytes(map_name.encode("ascii"))
                expected_by_map[map_name] = output.resolve()
                slot_files.append({
                    "map_index": map_index,
                    "map": map_name,
                    "role": "",
                    "path": str(output.resolve()),
                })
            override = {
                "7": {
                    "origin_kind": "blender_cluster_bake",
                    "texture_base": "",
                    "required_roles": [],
                    "files": {},
                    "slot_files": slot_files,
                },
            }
            override["7"]["origin_receipt"] = (
                self._write_blender_bake_receipt(
                    root,
                    production,
                    "7",
                    "M_branch_test_01",
                    slot_files,
                )
            )

            plan = build_spm_canonical_texture_plan(
                production,
                material_output_overrides=override,
            )

            self.assertEqual(plan["status"], "ok", plan["issues"])
            binding = plan["bindings"][0]
            self.assertEqual(binding["origin_kind"], "blender_cluster_bake")
            self.assertEqual(len(binding["slots"]), 8)
            self.assertNotEqual(
                expected_by_map["Gloss"],
                expected_by_map["AO"],
            )
            self.assertNotEqual(
                expected_by_map["SubsurfaceColor"],
                expected_by_map["SubsurfaceAmount"],
            )

            isolated = (
                root / ".sk_batch_isolated_bark" / "hash" / production.name
            )
            isolated.parent.mkdir(parents=True)
            isolated.write_bytes(production.read_bytes())
            result = rebase_spm_copy_to_canonical_outputs(
                isolated,
                production,
                plan,
            )
            slots = inspect_spm_texture_slots(isolated)["materials"][0]["slots"]

            self.assertEqual(result["rewritten_reference_count"], 8)
            self.assertEqual(
                {
                    row["map"]: Path(row["resolved_ref"]).resolve()
                    for row in slots
                },
                expected_by_map,
            )

    def test_blender_cluster_bake_rejects_receiptless_self_declaration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_test"
            production = root / "SK_tree_test_01.spm"
            self._write_spm(
                production,
                "M_branch_test_01",
                [("Color", "cluster/branch_test_01.tga")],
            )
            output = root / "cluster" / "branch_test_01.tga"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"color")

            plan = build_spm_canonical_texture_plan(
                production,
                material_output_overrides={
                    "7": {
                        "origin_kind": "blender_cluster_bake",
                        "texture_base": "",
                        "required_roles": [],
                        "files": {},
                        "slot_files": [{
                            "map_index": 0,
                            "map": "Color",
                            "role": "color",
                            "path": str(output),
                        }],
                    },
                },
            )

            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(
                plan["issues"][0]["reason"],
                "blender_cluster_bake_capture_manifest_missing",
            )

    def test_missing_bake_receipt_is_rebuilt_from_live_physical_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_test"
            production = root / "SK_tree_test_01.spm"
            self._write_spm(
                production,
                "M_branch_test_01",
                [
                    ("Color", "cluster/branch_test_01.tga"),
                    ("AO", "cluster/branch_test_01_AO.tga"),
                ],
            )
            slot_files = []
            for map_index, map_name in enumerate(("Color", "AO")):
                name = (
                    "branch_test_01.tga"
                    if map_name == "Color"
                    else "branch_test_01_AO.tga"
                )
                output = root / "cluster" / name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(map_name.encode("ascii"))
                slot_files.append({
                    "map_index": map_index,
                    "map": map_name,
                    "role": "color" if map_name == "Color" else "extra",
                    "path": str(output),
                })
            self._write_blender_bake_receipt(
                root,
                production,
                "7",
                "M_branch_test_01",
                slot_files,
            )

            plan = build_spm_canonical_texture_plan(
                production,
                material_output_overrides={
                    "7": {
                        "origin_kind": "blender_cluster_bake",
                        "texture_base": "",
                        "required_roles": [],
                        "files": {},
                        "slot_files": slot_files,
                    },
                },
            )

            self.assertEqual(plan["status"], "ok", plan["issues"])
            receipt = plan["bindings"][0]["origin_receipt"]
            self.assertEqual(
                plan["bindings"][0]["origin_state"],
                "blender_cluster_bake",
            )
            self.assertEqual(
                receipt["origin_state"],
                "blender_cluster_bake",
            )
            self.assertEqual(receipt["material_id"], "7")
            self.assertEqual(
                receipt["material_name"],
                "M_branch_test_01",
            )
            self.assertEqual(len(receipt["slot_files"]), 2)

    def test_blender_cluster_bake_rejects_wrong_ao_manifest_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree_test"
            production = root / "SK_tree_test_01.spm"
            self._write_spm(
                production,
                "M_branch_test_01",
                [("AO", "cluster/branch_test_01_AO.tga")],
            )
            output = root / "cluster" / "branch_test_01_AO.tga"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"ao")
            slot_files = [{
                "map_index": 0,
                "map": "AO",
                "role": "extra",
                "path": str(output),
            }]
            receipt = self._write_blender_bake_receipt(
                root,
                production,
                "7",
                "M_branch_test_01",
                slot_files,
                role_override="Gloss",
            )

            plan = build_spm_canonical_texture_plan(
                production,
                material_output_overrides={
                    "7": {
                        "origin_kind": "blender_cluster_bake",
                        "texture_base": "",
                        "required_roles": [],
                        "files": {},
                        "slot_files": slot_files,
                        "origin_receipt": receipt,
                    },
                },
            )

            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(
                plan["issues"][0]["reason"],
                "blender_cluster_bake_map_role_mismatch",
            )


if __name__ == "__main__":
    unittest.main()
