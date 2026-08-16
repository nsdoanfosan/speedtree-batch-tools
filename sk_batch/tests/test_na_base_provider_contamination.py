from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from na_base_provider_contamination import (  # noqa: E402
    NaBaseProviderContaminationError,
    rendered_provider_inventory,
    validate_base_provider_contamination,
)


def _receipt():
    return {
        "cluster_assembly": {
            "dependencies": [
                {
                    "name": "SK_branch_tree_01",
                    "role": "branch",
                    "target_material_names": ["branch_tree_01"],
                },
                {
                    "name": "SK_branch_tree_side_01",
                    "role": "branch",
                    "target_material_names": ["branch_tree_side_01"],
                },
                {
                    "name": "SK_leaf_tree_unused",
                    "role": "leaf",
                    "target_material_names": ["leaf_tree_unused"],
                },
            ],
        },
    }


def _base(material_names, polygon_material_indices):
    return SimpleNamespace(data=SimpleNamespace(
        materials=[SimpleNamespace(name=name) for name in material_names],
        polygons=[
            SimpleNamespace(material_index=index)
            for index in polygon_material_indices
        ],
    ))


class RenderedProviderInventoryTests(unittest.TestCase):
    def test_uses_actual_visible_spm_materials_not_selected_roles(self):
        inventory = rendered_provider_inventory(
            _receipt(),
            [
                "M_bark_tree_01",
                "M_branch_tree_01",
                "M_branch_tree_side_01",
            ],
        )

        self.assertEqual(
            [row["material_identity"] for row in inventory],
            ["branch_tree_01", "branch_tree_side_01"],
        )
        self.assertEqual(
            inventory[1]["providers"][0]["provider"],
            "SK_branch_tree_side_01",
        )

    def test_rejects_missing_authoritative_dependency_inventory(self):
        with self.assertRaisesRegex(
            NaBaseProviderContaminationError,
            "no cluster dependency inventory",
        ):
            rendered_provider_inventory(
                {"cluster_assembly": {}},
                ["M_branch_tree_01"],
            )


class NaBaseProviderContaminationTests(unittest.TestCase):
    def test_fails_closed_for_omitted_rendered_provider(self):
        inventory = rendered_provider_inventory(
            _receipt(),
            ["M_branch_tree_01", "M_branch_tree_side_01"],
        )
        base = _base(
            ["M_bark_tree_01", "M_branch_tree_side_01"],
            [0, 0, 1, 1, 1],
        )

        with self.assertRaisesRegex(
            NaBaseProviderContaminationError,
            r'"polygon_count": 3',
        ):
            validate_base_provider_contamination(base, inventory)

    def test_accepts_base_after_every_rendered_provider_is_removed(self):
        inventory = rendered_provider_inventory(
            _receipt(),
            ["M_branch_tree_01", "M_branch_tree_side_01"],
        )
        base = _base(["M_bark_tree_01"], [0, 0])

        report = validate_base_provider_contamination(base, inventory)

        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["rendered_provider_material_count"], 2)
        self.assertEqual(report["residual_polygon_count"], 0)


if __name__ == "__main__":
    unittest.main()
