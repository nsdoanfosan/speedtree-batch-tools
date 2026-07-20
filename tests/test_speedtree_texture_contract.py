import json
import tempfile
import unittest
from pathlib import Path

from speedtree_texture_contract import (
    REQUIRED_TEXTURE_ROLES,
    index_texture_sets,
    normalize_material_key,
    normalize_texture_set_key,
    parse_managed_texture_path,
    read_stmat_material_sources,
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


if __name__ == "__main__":
    unittest.main()
