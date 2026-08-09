import unittest

import generator_delivery_scope as scope


class GeneratorDeliveryScopeIdentityTests(unittest.TestCase):
    def test_guid_and_slot_prefix_are_exact_opaque_identities(self):
        upper = scope.canonical_slot_identity({
            "generator_guid": "AbC==",
            "slot_prefix": "Leaves:Type:100",
        })
        lower = scope.canonical_slot_identity({
            "generator_guid": "abc==",
            "slot_prefix": "leaves:type:100",
        })

        self.assertEqual(upper, ("guid", "AbC==", "Leaves:Type:100"))
        self.assertNotEqual(upper, lower)

    def test_named_fallback_normalizes_names_but_not_slot_prefix(self):
        first = scope.canonical_slot_identity({
            "generator_type": "Leaf Mesh",
            "generator_name": "Leaf A",
            "slot_prefix": "Material:Frond:900",
        })
        second = scope.canonical_slot_identity({
            "generator_type": "leaf mesh",
            "generator_name": "leaf a",
            "slot_prefix": "material:frond:900",
        })

        self.assertEqual(first[:3], second[:3])
        self.assertNotEqual(first, second)

    def test_material_default_mesh_sentinel_is_the_only_nonpositive_mesh(self):
        row = {
            "generator_guid": "AbC==",
            "slot_prefix": "Custom:Slot:5000",
            "target_material_id": 7,
            "target_mesh_id": -10,
        }
        self.assertEqual(
            scope.canonical_authored_slot(row)["target_mesh_id"],
            -10,
        )

        for invalid in (0, -1, -9, -11):
            with self.subTest(invalid=invalid):
                with self.assertRaises(scope.GeneratorDeliveryScopeError):
                    scope.canonical_authored_slot({
                        **row,
                        "target_mesh_id": invalid,
                    })


if __name__ == "__main__":
    unittest.main()
