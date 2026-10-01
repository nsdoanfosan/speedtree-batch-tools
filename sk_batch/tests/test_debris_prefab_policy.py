"""Only per-SPM selection activates; Wind/source naming never qualify."""
import unittest

from sk_batch.debris_prefab_policy import eligibility, native_eligibility


class DebrisPrefabPolicyTests(unittest.TestCase):
    def test_disabled_does_not_inspect_source(self):
        class UnreadableSource:
            def __str__(self):
                raise AssertionError("Do not inspect identity for admission")
        result = eligibility(UnreadableSource(), False)
        self.assertFalse(result["enabled"])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "disabled")

    def test_selected_arbitrary_names_categories_and_paths_are_candidates(self):
        sources = (
            "Ground_cover_A.spm", "SK_weed_deadleaves_future.spm",
            "SK_weed_deadbranches_future.spm", "SK_tree_deadleaves.spm",
            "D:/Art/OTHER_CATEGORY/RENAMED_SOURCE.SPM",
            "D:/Art/weed_deadbranches/Cluster/SK_unit.spm",
            "D:/Art/renamed/Cluster/anything.spm",
        )
        for source in sources:
            with self.subTest(source=source):
                result = eligibility(source, True)
                self.assertTrue(result["eligible"])
                self.assertIsNone(result["category"])
                self.assertEqual(result["mode"], "static_rest_pose")
                self.assertEqual(result["reason"], "eligible_explicit_selection")
                self.assertFalse(result["requires_actual_wind_verification"])

    def test_wind_is_metadata_for_any_explicitly_selected_source(self):
        values = (None, "TREE", "BUSH", "WEED", "GRASS", "NONE", "unknown",
                  {"preset": "NONE", "enabled": False},
                  {"preset": "NONE", "enabled": True},
                  {"preset": "TREE", "enabled": True},
                  {"preset": "BUSH", "enabled": False},
                  {"wind_preset": "WEED", "wind_enabled": True},
                  {"preset": "NONE"}, {"enabled": False}, {})
        for value in values:
            with self.subTest(wind=value):
                result = eligibility("Ground_cover_A.spm", True, value)
                self.assertTrue(result["eligible"])
                self.assertTrue(result["wind_audit_only"])
                native = native_eligibility("Ground_cover_A.spm", True, value,
                                            source_parts_verified=True,
                                            import_geometry_verified=True)
                self.assertTrue(native["eligible"])
                self.assertEqual(native["reason"], "eligible_native_verified")

    def test_report_retains_actual_wind_metadata_without_rewriting_it(self):
        contract = {"preset": "TREE", "enabled": True}
        before = dict(contract)
        result = eligibility("Renamed.spm", True, contract)
        self.assertEqual(contract, before)
        self.assertEqual(result["wind_preset"], "TREE")
        self.assertIs(result["wind_enabled"], True)
        alias = eligibility("Renamed.spm", True,
                            {"wind_preset": "NONE", "wind_enabled": False})
        self.assertEqual(alias["wind_preset"], "NONE")
        self.assertIs(alias["wind_enabled"], False)

    def test_native_gate_requires_actual_parts_and_import_geometry(self):
        for parts, geometry, reason in (
            (False, True, "source_parts_unverified"),
            (True, False, "import_geometry_unverified"),
            (1, True, "source_parts_unverified"),
            (True, 1, "import_geometry_unverified"),
        ):
            result = native_eligibility("Ground_cover_A.spm", True, None,
                                        source_parts_verified=parts,
                                        import_geometry_verified=geometry)
            self.assertFalse(result["eligible"])
            self.assertEqual(result["reason"], reason)
        result = native_eligibility("Ground_cover_A.spm", True, None,
                                    source_parts_verified=True,
                                    import_geometry_verified=True)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["gate_stage"], "native")

    def test_only_boolean_true_activates(self):
        for value in (False, None, 0, 1, "true", "false"):
            with self.subTest(value=value):
                result = eligibility("Ground_cover_A.spm", value)
                self.assertFalse(result["eligible"])
                self.assertEqual(result["reason"], "disabled" if value is False else
                                 "invalid_enabled_flag")

    def test_unselected_source_remains_disabled_with_valid_receipts(self):
        result = native_eligibility("Ground_cover_A.spm", False,
                                    {"preset": "NONE", "enabled": False},
                                    source_parts_verified=True,
                                    import_geometry_verified=True)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "disabled")


if __name__ == "__main__":
    unittest.main()
