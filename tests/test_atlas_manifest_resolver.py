import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from atlas_manifest_resolver import (
    AtlasManifestResolutionError,
    GENERATOR_BINDING_OWNERSHIP_CONTRACT,
    GENERATOR_BINDING_OWNERSHIP_VERSION,
    GENERATOR_SLOT_CREATION_PROVENANCE_CONTRACT,
    GENERATOR_SLOT_CREATION_PROVENANCE_VERSION,
    atlas_manifest_mirror_repair_plan,
    diagnose_manifest_generator_candidates,
    repair_atlas_manifest_mirrors,
    resolution_evidence,
    resolve_atlas_manifests,
    resolve_manifest_material_ownership,
    generator_binding_ownership_fingerprint,
    generator_slot_creation_provenance_fingerprint,
)


class AtlasManifestResolverTests(unittest.TestCase):
    def ownership_block(self, bindings):
        return {
            "contract": GENERATOR_BINDING_OWNERSHIP_CONTRACT,
            "version": GENERATOR_BINDING_OWNERSHIP_VERSION,
            "binding_count": len(bindings),
            "fingerprint": generator_binding_ownership_fingerprint(bindings),
            "bindings": copy.deepcopy(bindings),
        }

    def creation_provenance_block(self, slots):
        return {
            "contract": GENERATOR_SLOT_CREATION_PROVENANCE_CONTRACT,
            "version": GENERATOR_SLOT_CREATION_PROVENANCE_VERSION,
            "slot_count": len(slots),
            "fingerprint": generator_slot_creation_provenance_fingerprint(
                slots
            ),
            "slots": copy.deepcopy(slots),
        }

    def fixture(self, temporary):
        root = Path(temporary) / "Tree_test"
        root.mkdir()
        target = root / "SK_Tree_test_01.spm"
        target.write_bytes(b"spm")
        blend = root / "atlas" / "leaf_test.blend"
        blend.parent.mkdir()
        blend.write_bytes(b"blend")
        payload = {
            "atlas_manifest_schema_version": 1,
            "spm": str(target),
            "blend_file": str(blend),
            "source_collection": "Leaf Test",
            "export_scope_id": "scope-leaf-test",
            "material_groups": [{
                "material": "M_leaf_test",
                "material_id": 7,
                "mesh_ids": [20],
            }],
            "generator_connection": {
                "complete": True,
                "bindings": [{
                    "generator_guid": "leaf-guid",
                    "slot_prefix": "Leaves:Type:0",
                    "target_material_id": 7,
                    "target_mesh_id": 20,
                }],
            },
        }
        return root, target, blend, payload

    def write_candidate(self, root, target, kind, payload, *, name=None):
        if kind == "exact_per_target":
            directory = root / ".atlas_leaf_speedtree_targets"
            directory.mkdir(exist_ok=True)
            path = directory / (name or f"{target.stem}.json")
        elif kind == "exact_target_scope":
            directory = root / ".atlas_leaf_speedtree_scopes"
            directory.mkdir(exist_ok=True)
            path = directory / (name or f"scope__{target.stem}.json")
        elif kind == "exact_global_target":
            path = root / "speedtree_import_manifest.json"
        elif kind == "legacy_material_manifest":
            path = root / (name or "speedtree_import_manifest_M_leaf_test.json")
        else:
            raise AssertionError(kind)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def precedence_fixture(self, temporary, fixture_name):
        fixture_path = Path(__file__).parent / "fixtures" / fixture_name
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        root = Path(temporary) / fixture["asset_folder"]
        root.mkdir()
        target = root / fixture["target_spm"]
        target.write_bytes(b"sanitized-spm")

        def materialize(value):
            if isinstance(value, dict):
                return {key: materialize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [materialize(item) for item in value]
            if isinstance(value, str):
                return value.replace("{root}", str(root)).replace(
                    "{target}", str(target)
                )
            return value

        current = materialize(fixture["current_per_target"])
        stale = materialize(fixture["stale_target_scope"])
        authority_path = self.write_candidate(
            root,
            target,
            "exact_per_target",
            current,
        )
        mirror_path = self.write_candidate(
            root,
            target,
            "exact_target_scope",
            current,
            name=f"{current['export_scope_id']}__{target.stem}.json",
        )
        stale_path = self.write_candidate(
            root,
            target,
            "exact_target_scope",
            stale,
            name=f"{stale['export_scope_id']}__{target.stem}.json",
        )

        # Deliberately make the stale scope newest.  File time is evidence,
        # never authority.
        os.utime(authority_path, (1, 1))
        os.utime(mirror_path, (2, 2))
        os.utime(stale_path, (3, 3))
        return {
            "fixture": fixture,
            "target": target,
            "current": current,
            "stale": stale,
            "authority_path": authority_path,
            "mirror_path": mirror_path,
            "stale_path": stale_path,
        }

    def assert_stale_scope_is_diagnosed(self, case, live_bindings):
        resolution = resolve_atlas_manifests(case["target"])
        self.assertEqual(
            [row["path"] for row in resolution["selected"]],
            [
                str(case["authority_path"].resolve()),
                str(case["mirror_path"].resolve()),
                str(case["stale_path"].resolve()),
            ],
        )
        connection = resolution["selected"][0]["payload"][
            "generator_connection"
        ]
        self.assertFalse(connection["requested"])
        self.assertEqual(connection["bindings"], [])
        diagnostics = diagnose_manifest_generator_candidates(
            resolution,
            live_bindings,
        )
        conflict = next(
            row
            for row in diagnostics["conflicting"]
            if row["path"] == str(case["stale_path"].resolve())
        )
        self.assertEqual(
            conflict["status"],
            "manifest_candidate_live_conflict",
        )
        self.assertEqual(diagnostics["status"], "conflicting")
        self.assertEqual(resolution["conflicting"], [])
        return resolution, conflict

    def test_candidate_kind_and_precedence_matrix(self):
        kinds = (
            "exact_per_target",
            "exact_target_scope",
            "exact_global_target",
        )
        for kind in kinds:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root, target, _blend, payload = self.fixture(temporary)
                path = self.write_candidate(root, target, kind, payload)

                resolution = resolve_atlas_manifests(target)

                self.assertEqual(
                    [(row["kind"], row["path"]) for row in resolution["selected"]],
                    [(kind, str(path.resolve()))],
                )
                self.assertEqual(resolution["rejected"], [])
                self.assertEqual(resolution["conflicting"], [])

        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            paths = [
                self.write_candidate(root, target, kind, payload)
                for kind in kinds
            ]

            resolution = resolve_atlas_manifests(target)

            self.assertEqual(
                [row["kind"] for row in resolution["selected"]],
                list(kinds),
            )
            self.assertEqual(
                [row["path"] for row in resolution["selected"]],
                [str(path.resolve()) for path in paths],
            )
            self.assertEqual(
                [row["reason"] for row in resolution["selected"]],
                [
                    "selected_authority",
                    "coherent_operational_mirror",
                    "coherent_operational_mirror",
                ],
            )

    def test_missing_candidate_matrix_is_explicit_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            _root, target, _blend, _payload = self.fixture(temporary)

            first = resolve_atlas_manifests(target)
            second = resolve_atlas_manifests(target)

            self.assertEqual(first, second)
            self.assertEqual(first["selected"], [])
            self.assertEqual(
                {row["reason"] for row in first["missing"]},
                {
                    "candidate_file_missing",
                    "target_scope_candidate_missing",
                },
            )
            self.assertEqual(len(first["missing"]), 3)

    def test_disjoint_scope_owners_can_coexist_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, blend, payload = self.fixture(temporary)
            other = json.loads(json.dumps(payload))
            other["blend_file"] = str(blend.with_name("branch_test.blend"))
            other["source_collection"] = "Branch Test"
            other["export_scope_id"] = "scope-branch-test"
            other["material_groups"] = [{
                "material": "M_branch_test",
                "material_id": 8,
                "mesh_ids": [30],
            }]
            other["generator_connection"] = {"complete": True, "bindings": []}
            first = self.write_candidate(
                root,
                target,
                "exact_target_scope",
                payload,
                name=f"a__{target.stem}.json",
            )
            second = self.write_candidate(
                root,
                target,
                "exact_target_scope",
                other,
                name=f"b__{target.stem}.json",
            )

            resolution = resolve_atlas_manifests(target)

            self.assertEqual(
                [row["path"] for row in resolution["selected"]],
                [str(first.resolve()), str(second.resolve())],
            )
            self.assertEqual(resolution["conflicting"], [])

    def test_explicit_current_ownership_is_sole_authority_including_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, blend, historical = self.fixture(temporary)
            historical["generator_connection"]["requested"] = True
            historical["generator_binding_ownership"] = self.ownership_block([])
            historical["generator_slot_creation_provenance"] = (
                self.creation_provenance_block(
                    historical["generator_connection"]["bindings"]
                )
            )

            current = copy.deepcopy(historical)
            current["blend_file"] = str(blend.with_name("current.blend"))
            current["source_collection"] = "Current Provider"
            current["export_scope_id"] = "scope-current"
            current["material_groups"] = [{
                "material": "M_current",
                "material_id": 8,
                "mesh_ids": [30, 31],
            }]
            current["generator_connection"]["requested"] = False
            current_binding = {
                "generator_guid": "leaf-guid",
                "generator_name": "Renamed Diagnostic",
                "slot_prefix": "Leaves:Type:0",
                "target_material_id": 8,
                "target_mesh_id": 30,
                "state": "later_takeover",
            }
            current["generator_binding_ownership"] = self.ownership_block(
                [current_binding]
            )

            self.write_candidate(
                root,
                target,
                "exact_target_scope",
                historical,
                name=f"scope-old__{target.stem}.json",
            )
            self.write_candidate(
                root,
                target,
                "exact_target_scope",
                current,
                name=f"scope-current__{target.stem}.json",
            )

            resolution = resolve_atlas_manifests(target)
            generator_claims = [
                claim
                for row in resolution["selected"]
                for claim in row["ownership_claims"]
                if claim.startswith("generator:")
            ]
            self.assertEqual(
                generator_claims,
                ["generator:guid:leaf-guid:slot:Leaves:Type:0"],
            )
            self.assertEqual(
                resolution["selected"][0]["payload"][
                    "generator_connection"
                ]["bindings"],
                historical["generator_connection"]["bindings"],
            )
            diagnostics = diagnose_manifest_generator_candidates(
                resolution,
                [{
                    "generator_guid": "leaf-guid",
                    "slot_prefix": "Leaves:Type:0",
                    "material_id": 8,
                    "mesh_id": 30,
                    "visible": True,
                    "export_participates": True,
                }],
            )
            self.assertEqual(diagnostics["status"], "coherent")
            current_diagnostic = next(
                row for row in diagnostics["candidates"]
                if row["path"].endswith(
                    f"scope-current__{target.stem}.json"
                )
            )
            self.assertEqual(
                current_diagnostic["status"], "live_coherent"
            )
            self.assertEqual(
                len(current_diagnostic["binding_results"]), 1
            )
            # Mesh 31 is a legitimate generated provider asset even though no
            # current slot selects it.
            self.assertFalse(any(
                row.get("non_export_participating_mesh_ids")
                for row in diagnostics["candidates"]
            ))

    def test_explicit_claim_projection_ignores_run_and_creator_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            binding = copy.deepcopy(
                payload["generator_connection"]["bindings"][0]
            )
            binding.update({
                "state": "changed",
                "created_slot": True,
                "source_material_id": 70,
                "source_mesh_id": 200,
            })
            payload["generator_binding_ownership"] = self.ownership_block(
                [binding]
            )
            payload["generator_slot_creation_provenance"] = (
                self.creation_provenance_block([binding])
            )
            mirror = copy.deepcopy(payload)
            mirror_binding = mirror["generator_binding_ownership"][
                "bindings"
            ][0]
            mirror_binding.update({
                "state": "already_connected",
                "created_slot": False,
                "generator_index": 999,
                "generator_name": "Diagnostic Rename",
                "source_material_id": None,
                "source_mesh_id": None,
            })
            mirror["generator_slot_creation_provenance"]["slots"][0][
                "created_property_names"
            ] = ["historical-only-drift"]
            mirror["generator_slot_creation_provenance"]["fingerprint"] = (
                generator_slot_creation_provenance_fingerprint(
                    mirror["generator_slot_creation_provenance"]["slots"]
                )
            )

            self.write_candidate(root, target, "exact_per_target", payload)
            self.write_candidate(root, target, "exact_global_target", mirror)

            resolution = resolve_atlas_manifests(target)
            self.assertEqual(len(resolution["selected"]), 2)
            self.assertEqual(
                [row["reason"] for row in resolution["selected"]],
                ["selected_authority", "coherent_operational_mirror"],
            )

    def test_generator_guid_and_slot_identity_remain_exact_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, blend, payload = self.fixture(temporary)
            first = copy.deepcopy(payload)
            first["generator_binding_ownership"] = self.ownership_block([{
                "generator_guid": "AbC+/=",
                "slot_prefix": "Leaves:Type:7",
                "target_material_id": 7,
                "target_mesh_id": 20,
            }])
            second = copy.deepcopy(payload)
            second["blend_file"] = str(blend.with_name("case-two.blend"))
            second["source_collection"] = "Case Two"
            second["export_scope_id"] = "scope-case-two"
            second["material_groups"] = [{
                "material": "M_case_two",
                "material_id": 8,
                "mesh_ids": [30],
            }]
            second["generator_binding_ownership"] = self.ownership_block([{
                "generator_guid": "aBc+/=",
                "slot_prefix": "leaves:type:7",
                "target_material_id": 8,
                "target_mesh_id": 30,
            }])
            self.write_candidate(
                root, target, "exact_target_scope", first,
                name=f"case-a__{target.stem}.json",
            )
            self.write_candidate(
                root, target, "exact_target_scope", second,
                name=f"case-b__{target.stem}.json",
            )

            resolution = resolve_atlas_manifests(target)
            claims = {
                claim
                for row in resolution["selected"]
                for claim in row["ownership_claims"]
                if claim.startswith("generator:")
            }
            self.assertEqual(claims, {
                "generator:guid:AbC+/=:slot:Leaves:Type:7",
                "generator:guid:aBc+/=:slot:leaves:type:7",
            })

    def test_current_ownership_fingerprint_has_one_canonical_algorithm(self):
        bindings = [
            {
                "generator_guid": " QWxwaGE= ",
                "generator_index": 99,
                "generator_name": "ignored",
                "slot_prefix": " Material:Frond:900 ",
                "target_material_id": "40",
                "target_mesh_id": "4900",
                "state": "ignored",
            },
            {
                "generator_guid": "AbC+/=",
                "slot_prefix": "Leaves:Type:42",
                "target_material_id": 7,
                "target_mesh_id": 20,
                "created_slot": True,
            },
        ]
        self.assertEqual(
            generator_binding_ownership_fingerprint(bindings),
            "fe3eb6d9c1aae51fdd7b3831edff66537cc496428fd266ddca4de02c806aea9a",
        )

    def test_live_audit_never_falls_back_past_declared_guid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            payload["generator_binding_ownership"] = self.ownership_block([{
                "generator_guid": "Leaf-GUID",
                "generator_index": 7,
                "generator_name": "Leaf",
                "slot_prefix": "Leaves:Type:42",
                "target_material_id": 7,
                "target_mesh_id": 20,
            }])
            self.write_candidate(
                root, target, "exact_target_scope", payload
            )
            resolution = resolve_atlas_manifests(target)
            live = {
                "generator_index": 7,
                "generator_name": "Leaf",
                "slot_prefix": "Leaves:Type:42",
                "material_id": 7,
                "mesh_id": 20,
            }
            for guid in ("", "leaf-GUID"):
                with self.subTest(guid=guid):
                    current = {**live, "generator_guid": guid}
                    diagnostic = diagnose_manifest_generator_candidates(
                        resolution, [current]
                    )
                    self.assertEqual(diagnostic["status"], "conflicting")
                    self.assertIn(
                        "generator_slot_missing",
                        diagnostic["conflicting"][0]["reasons"],
                    )

            diagnostic = diagnose_manifest_generator_candidates(
                resolution,
                [{**live, "generator_guid": "Leaf-GUID"}],
            )
            self.assertEqual(diagnostic["status"], "coherent")

    def test_explicit_overlapping_current_owners_still_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, blend, payload = self.fixture(temporary)
            first = copy.deepcopy(payload)
            first["generator_binding_ownership"] = self.ownership_block([{
                "generator_guid": "leaf-guid",
                "slot_prefix": "Leaves:Type:42",
                "target_material_id": 7,
                "target_mesh_id": 20,
            }])
            second = copy.deepcopy(first)
            second["blend_file"] = str(blend.with_name("other.blend"))
            second["source_collection"] = "Other Provider"
            second["export_scope_id"] = "scope-other"
            second["material_groups"] = [{
                "material": "M_other",
                "material_id": 8,
                "mesh_ids": [30],
            }]
            second["generator_binding_ownership"] = self.ownership_block([{
                "generator_guid": "leaf-guid",
                "slot_prefix": "Leaves:Type:42",
                "target_material_id": 8,
                "target_mesh_id": 30,
            }])
            self.write_candidate(
                root, target, "exact_target_scope", first,
                name=f"first__{target.stem}.json",
            )
            other = self.write_candidate(
                root, target, "exact_target_scope", second,
                name=f"other__{target.stem}.json",
            )

            with self.assertRaises(AtlasManifestResolutionError) as caught:
                resolve_atlas_manifests(target)

            conflict = caught.exception.resolution["conflicting"][0]
            self.assertEqual(
                conflict["reason"], "operational_candidate_disagreement"
            )
            self.assertEqual(conflict["path"], str(other.resolve()))
            self.assertEqual(
                conflict["ownership_claim"],
                "generator:guid:leaf-guid:slot:Leaves:Type:42",
            )

    def test_explicit_current_ownership_contract_errors_fail_closed(self):
        valid_binding = {
            "generator_guid": "leaf-guid",
            "slot_prefix": "Leaves:Type:42",
            "target_material_id": 7,
            "target_mesh_id": 20,
        }
        valid = self.ownership_block([valid_binding])
        variants = []

        def variant(reason, mutate):
            block = copy.deepcopy(valid)
            mutate(block)
            variants.append((reason, block))

        variants.append(("generator_binding_ownership_not_object", []))
        variant(
            "generator_binding_ownership_contract_invalid",
            lambda block: block.__setitem__("contract", "foreign"),
        )
        variant(
            "generator_binding_ownership_version_invalid",
            lambda block: block.__setitem__("version", "1"),
        )
        variant(
            "generator_binding_ownership_version_unsupported",
            lambda block: block.__setitem__("version", 2),
        )
        variant(
            "generator_binding_ownership_bindings_not_list",
            lambda block: block.__setitem__("bindings", {}),
        )
        variant(
            "generator_binding_ownership_binding_count_invalid",
            lambda block: block.__setitem__("binding_count", 99),
        )
        variant(
            "generator_binding_ownership_binding_not_object",
            lambda block: block.__setitem__("bindings", ["bad"]),
        )
        variant(
            "generator_binding_ownership_generator_guid_missing",
            lambda block: block["bindings"][0].__setitem__(
                "generator_guid", ""
            ),
        )
        variant(
            "generator_binding_ownership_slot_prefix_missing",
            lambda block: block["bindings"][0].__setitem__(
                "slot_prefix", ""
            ),
        )
        variant(
            "generator_binding_ownership_target_material_id_invalid",
            lambda block: block["bindings"][0].__setitem__(
                "target_material_id", "not-an-int"
            ),
        )
        for invalid_material_id in (0, -1, -10):
            variant(
                "generator_binding_ownership_target_material_id_invalid",
                lambda block, value=invalid_material_id: block[
                    "bindings"
                ][0].__setitem__("target_material_id", value),
            )
        variant(
            "generator_binding_ownership_target_mesh_id_invalid",
            lambda block: block["bindings"][0].__setitem__(
                "target_mesh_id", "not-an-int"
            ),
        )
        for invalid_mesh_id in (0, -1, -11):
            variant(
                "generator_binding_ownership_target_mesh_id_invalid",
                lambda block, value=invalid_mesh_id: block["bindings"][
                    0
                ].__setitem__("target_mesh_id", value),
            )
        duplicate = copy.deepcopy(valid_binding)
        duplicate["target_mesh_id"] = 21
        variant(
            "generator_binding_ownership_duplicate_slot",
            lambda block: (
                block["bindings"].append(duplicate),
                block.__setitem__("binding_count", 2),
            ),
        )
        variant(
            "generator_binding_ownership_fingerprint_invalid",
            lambda block: block.__setitem__("fingerprint", "0" * 64),
        )
        variant(
            "generator_binding_ownership_fingerprint_invalid",
            lambda block: block.__setitem__(
                "fingerprint", block["fingerprint"].upper()
            ),
        )

        for reason, ownership in variants:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root, target, _blend, payload = self.fixture(temporary)
                payload["generator_binding_ownership"] = ownership
                self.write_candidate(
                    root, target, "exact_target_scope", payload
                )

                with self.assertRaises(AtlasManifestResolutionError) as caught:
                    resolve_atlas_manifests(target)

                self.assertEqual(
                    caught.exception.resolution["conflicting"][0]["reason"],
                    reason,
                )

    def test_target_mesh_default_cutout_sentinel_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            binding = copy.deepcopy(
                payload["generator_connection"]["bindings"][0]
            )
            binding["target_mesh_id"] = -10
            payload["generator_binding_ownership"] = self.ownership_block(
                [binding]
            )
            self.write_candidate(
                root, target, "exact_target_scope", payload
            )

            resolution = resolve_atlas_manifests(target)

            self.assertEqual(
                resolution["selected"][0]["payload"][
                    "generator_binding_ownership"
                ]["bindings"][0]["target_mesh_id"],
                -10,
            )

    def test_invalid_legacy_target_id_domains_fail_closed(self):
        cases = (
            (
                "target_material_id",
                0,
                "legacy_generator_binding_target_material_id_invalid",
            ),
            (
                "target_mesh_id",
                0,
                "legacy_generator_binding_target_mesh_id_invalid",
            ),
            (
                "target_mesh_id",
                -1,
                "legacy_generator_binding_target_mesh_id_invalid",
            ),
        )
        for field, value, reason in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                root, target, _blend, payload = self.fixture(temporary)
                payload["generator_connection"]["bindings"][0][field] = value
                self.write_candidate(
                    root, target, "exact_target_scope", payload
                )

                with self.assertRaises(AtlasManifestResolutionError) as caught:
                    resolve_atlas_manifests(target)

                self.assertEqual(
                    caught.exception.resolution["conflicting"][0]["reason"],
                    reason,
                )

    def test_explicit_creation_provenance_contract_errors_fail_closed(self):
        valid_slot = {
            "generator_guid": "leaf-guid",
            "slot_prefix": "Leaves:Type:42",
            "created_property_names": [
                "Leaves:Type:42:Material",
                "Leaves:Type:42:Mesh",
            ],
        }
        valid = self.creation_provenance_block([valid_slot])
        variants = []

        def variant(reason, mutate):
            block = copy.deepcopy(valid)
            mutate(block)
            variants.append((reason, block))

        variants.append(("generator_slot_creation_provenance_not_object", []))
        variant(
            "generator_slot_creation_provenance_contract_invalid",
            lambda block: block.__setitem__("contract", "foreign"),
        )
        variant(
            "generator_slot_creation_provenance_version_invalid",
            lambda block: block.__setitem__("version", "1"),
        )
        variant(
            "generator_slot_creation_provenance_version_unsupported",
            lambda block: block.__setitem__("version", 2),
        )
        variant(
            "generator_slot_creation_provenance_slots_not_list",
            lambda block: block.__setitem__("slots", {}),
        )
        variant(
            "generator_slot_creation_provenance_slot_count_invalid",
            lambda block: block.__setitem__("slot_count", 99),
        )
        variant(
            "generator_slot_creation_provenance_slot_not_object",
            lambda block: block.__setitem__("slots", ["bad"]),
        )
        variant(
            "generator_slot_creation_provenance_generator_guid_missing",
            lambda block: block["slots"][0].__setitem__(
                "generator_guid", ""
            ),
        )
        variant(
            "generator_slot_creation_provenance_slot_prefix_missing",
            lambda block: block["slots"][0].__setitem__(
                "slot_prefix", ""
            ),
        )
        duplicate = copy.deepcopy(valid_slot)
        duplicate["created_property_names"] = ["different"]
        variant(
            "generator_slot_creation_provenance_duplicate_slot",
            lambda block: (
                block["slots"].append(duplicate),
                block.__setitem__("slot_count", 2),
            ),
        )
        variant(
            "generator_slot_creation_provenance_fingerprint_invalid",
            lambda block: block.__setitem__("fingerprint", "0" * 64),
        )
        variant(
            "generator_slot_creation_provenance_fingerprint_invalid",
            lambda block: block.__setitem__(
                "fingerprint", block["fingerprint"].upper()
            ),
        )

        for reason, provenance in variants:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root, target, _blend, payload = self.fixture(temporary)
                payload["generator_slot_creation_provenance"] = provenance
                self.write_candidate(
                    root, target, "exact_target_scope", payload
                )

                with self.assertRaises(AtlasManifestResolutionError) as caught:
                    resolve_atlas_manifests(target)

                self.assertEqual(
                    caught.exception.resolution["conflicting"][0]["reason"],
                    reason,
                )

    def test_issue163_arbitrary_slots_and_repeated_provider_takeovers(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "issue163_arbitrary_slot_takeovers.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        creator_fingerprints = {}
        observed_type7_owners = []
        for phase in fixture["phases"]:
            with self.subTest(phase=phase["name"]), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / fixture["target_spm"]
                target.write_bytes(b"sanitized-spm")
                expected_claims = set()
                provider_by_scope = {
                    provider["scope"]: provider
                    for provider in fixture["providers"]
                }
                for provider in fixture["providers"]:
                    current = copy.deepcopy(
                        phase.get("owners", {}).get(provider["scope"], [])
                    )
                    payload = {
                        "spm": str(target),
                        "blend_file": str(root / provider["blend"]),
                        "source_collection": provider["collection"],
                        "export_scope_id": provider["scope"],
                        "material_groups": [{
                            "material": provider["material"],
                            "material_id": provider["material_id"],
                            "mesh_ids": provider["mesh_ids"],
                        }],
                        "generator_connection": {
                            "requested": True,
                            "complete": True,
                            "bindings": copy.deepcopy(
                                provider["legacy_bindings"]
                            ),
                        },
                        "generator_binding_ownership": self.ownership_block(
                            current
                        ),
                        "generator_slot_creation_provenance": (
                            self.creation_provenance_block(
                                provider["created_slots"]
                            )
                        ),
                    }
                    path = self.write_candidate(
                        root,
                        target,
                        "exact_target_scope",
                        payload,
                        name=f"{provider['scope']}__{target.stem}.json",
                    )
                    for binding in current:
                        expected_claims.add(
                            "generator:guid:"
                            f"{binding['generator_guid']}:slot:"
                            f"{binding['slot_prefix']}"
                        )
                    creator_fingerprints.setdefault(
                        provider["scope"],
                        payload["generator_slot_creation_provenance"][
                            "fingerprint"
                        ],
                    )
                    self.assertEqual(
                        json.loads(path.read_text(encoding="utf-8"))[
                            "generator_connection"
                        ]["bindings"],
                        provider["legacy_bindings"],
                    )

                resolution = resolve_atlas_manifests(target)
                self.assertEqual(len(resolution["selected"]), 4)
                actual_claims = {
                    claim
                    for row in resolution["selected"]
                    for claim in row["ownership_claims"]
                    if claim.startswith("generator:")
                }
                self.assertEqual(actual_claims, expected_claims)
                if phase["name"] in {
                    "b_to_c_and_add_high",
                    "return_to_a_with_three_providers",
                }:
                    self.assertTrue(any(
                        "Type:100" in row for row in actual_claims
                    ))
                current_type7 = [
                    scope
                    for scope, bindings in phase.get("owners", {}).items()
                    if any(
                        binding["slot_prefix"] == "Leaves:Type:7"
                        for binding in bindings
                    )
                ]
                observed_type7_owners.append(current_type7[0])

                live = [
                    {
                        "generator_guid": binding["generator_guid"],
                        "slot_prefix": binding["slot_prefix"],
                        "material_id": binding["target_material_id"],
                        "mesh_id": binding["target_mesh_id"],
                        "visible": True,
                        "export_participates": True,
                    }
                    for bindings in phase.get("owners", {}).values()
                    for binding in bindings
                ]
                diagnostics = diagnose_manifest_generator_candidates(
                    resolution, live
                )
                self.assertEqual(diagnostics["status"], "coherent")
                for scope, expected in creator_fingerprints.items():
                    provider = provider_by_scope[scope]
                    self.assertEqual(
                        expected,
                        generator_slot_creation_provenance_fingerprint(
                            provider["created_slots"]
                        ),
                    )

        self.assertEqual(
            observed_type7_owners,
            ["scope-a", "scope-b", "scope-c", "scope-a"],
        )

    def test_silky_candidate_conflict_precedes_asset_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = self.precedence_fixture(
                temporary,
                "issue58_silky_generator_precedence.json",
            )
            _resolution, conflict = self.assert_stale_scope_is_diagnosed(
                case,
                case["fixture"]["live_generator_topology"],
            )

            stale_slots = {
                row["slot_prefix"]
                for row in case["stale"]["generator_connection"]["bindings"]
            }
            live_slots = {
                row["slot_prefix"]
                for row in case["fixture"]["live_generator_topology"]
            }
            self.assertIn("Leaves:Type:3", stale_slots)
            self.assertNotIn("Leaves:Type:3", live_slots)
            self.assertIn("Material:Frond:0", live_slots)
            self.assertTrue({
                "generator_slot_missing",
                "target_mesh_mismatch",
            }.intersection(conflict["reasons"]))

    def test_black_locust_candidate_conflict_precedes_asset_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = self.precedence_fixture(
                temporary,
                "issue58_black_locust_generator_precedence.json",
            )
            live = case["fixture"]["live_generator_snapshot"]
            _resolution, conflict = self.assert_stale_scope_is_diagnosed(
                case,
                live["bindings"],
            )
            self.assertFalse(live["node_table"]["stale"])
            self.assertEqual(live["node_table"]["orphan_node_count"], 0)
            visible_meshes = {
                row["mesh_id"]
                for row in live["bindings"]
                if row["export_participates"]
            }
            hidden_declared_meshes = {
                row["mesh_id"]
                for row in live["bindings"]
                if row["generator_name"] == "Leaf 12"
                and not row["export_participates"]
            }
            self.assertEqual(visible_meshes, {93})
            self.assertEqual(hidden_declared_meshes, {93, 94, 95, 96})
            self.assertIn(
                "declared_mesh_not_export_participating",
                conflict["reasons"],
            )

    def test_birch_multi_provider_receipts_all_prove_live_ownership(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "issue58_birch_multi_provider_precedence.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / fixture["asset_folder"]
            root.mkdir()
            target = root / fixture["target_spm"]
            target.write_bytes(b"sanitized-spm")

            def materialize(value):
                if isinstance(value, dict):
                    return {
                        key: materialize(item) for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [materialize(item) for item in value]
                if isinstance(value, str):
                    return value.replace("{root}", str(root)).replace(
                        "{target}", str(target)
                    )
                return value

            providers = fixture["providers"]
            payloads = []
            for index, provider in enumerate(providers):
                if "mirror_of" in provider:
                    payload = json.loads(json.dumps(
                        payloads[int(provider["mirror_of"])]
                    ))
                else:
                    payload = materialize(provider["payload"])
                payloads.append(payload)
                self.write_candidate(
                    root,
                    target,
                    provider["kind"],
                    payload,
                    name=provider.get("name"),
                )

            resolution = resolve_atlas_manifests(target)
            ownership = resolve_manifest_material_ownership(
                resolution,
                materialize(fixture["live_materials"]),
                target_spm=target,
            )
            diagnostics = diagnose_manifest_generator_candidates(
                resolution,
                fixture["live_generator_topology"],
            )

            self.assertEqual(len(resolution["selected"]), 5)
            self.assertEqual(ownership["status"], "proven")
            self.assertEqual(
                {row["material_id"] for row in ownership["materials"]},
                {"16", "17", "18", "19"},
            )
            self.assertEqual(diagnostics["status"], "coherent")
            self.assertEqual(diagnostics["conflicting"], [])

    def test_every_operational_precedence_disagreement_fails_closed(self):
        variants = {}
        with tempfile.TemporaryDirectory() as temporary:
            root, target, blend, payload = self.fixture(temporary)
            material = json.loads(json.dumps(payload))
            material["material_groups"][0]["mesh_ids"] = [99]
            variants["material_group"] = material

            source = json.loads(json.dumps(payload))
            source["blend_file"] = str(blend.with_name("copied.blend"))
            variants["source_identity"] = source

            binding = json.loads(json.dumps(payload))
            binding["generator_connection"]["bindings"][0][
                "target_mesh_id"
            ] = 99
            variants["generator_binding"] = binding

            for label, conflicting_payload in variants.items():
                with self.subTest(label=label):
                    target_dir = root / ".atlas_leaf_speedtree_targets"
                    if target_dir.exists():
                        for path in target_dir.glob("*.json"):
                            path.unlink()
                    global_path = root / "speedtree_import_manifest.json"
                    if global_path.exists():
                        global_path.unlink()
                    authority = self.write_candidate(
                        root,
                        target,
                        "exact_per_target",
                        payload,
                    )
                    conflict = self.write_candidate(
                        root,
                        target,
                        "exact_global_target",
                        conflicting_payload,
                    )

                    with self.assertRaises(AtlasManifestResolutionError) as caught:
                        resolve_atlas_manifests(target)

                    evidence = caught.exception.resolution
                    self.assertEqual(
                        evidence["conflicting"][0]["reason"],
                        "operational_candidate_disagreement",
                    )
                    self.assertEqual(
                        evidence["conflicting"][0]["path"],
                        str(conflict.resolve()),
                    )
                    self.assertEqual(
                        evidence["conflicting"][0]["conflicts_with"],
                        str(authority.resolve()),
                    )

    def test_unsupported_operational_schema_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            unsupported = dict(payload)
            unsupported["atlas_manifest_schema_version"] = 2
            path = self.write_candidate(
                root,
                target,
                "exact_target_scope",
                unsupported,
            )

            with self.assertRaises(AtlasManifestResolutionError) as caught:
                resolve_atlas_manifests(target)

            self.assertEqual(caught.exception.resolution["conflicting"], [{
                "path": str(path.resolve()),
                "kind": "exact_target_scope",
                "precedence": 1,
                "reason": "unsupported_schema_version",
                "schema": {
                    "field": "atlas_manifest_schema_version",
                    "version": 2,
                    "status": "unsupported",
                    "reason": "unsupported_schema_version",
                },
            }])

    def test_exact_authority_can_repair_same_source_stale_mirror(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            authority = self.write_candidate(
                root, target, "exact_per_target", payload
            )
            stale = copy.deepcopy(payload)
            stale["material_groups"][0]["mesh_ids"] = [99]
            mirror = self.write_candidate(
                root, target, "exact_global_target", stale
            )

            plan = atlas_manifest_mirror_repair_plan(target)
            self.assertEqual(plan["status"], "repairable", plan)
            self.assertEqual(
                plan["reason_code"],
                "atlas_manifest_mirror_conflict_repairable",
            )
            self.assertEqual(plan["authority"], str(authority.resolve()))
            self.assertEqual(plan["mirrors"], [str(mirror.resolve())])

            result = repair_atlas_manifest_mirrors(target)
            self.assertEqual(result["status"], "repaired")
            resolution = resolve_atlas_manifests(target)
            self.assertEqual(len(resolution["selected"]), 2)
            self.assertEqual(resolution["conflicting"], [])
            repaired = json.loads(mirror.read_text(encoding="utf-8"))
            self.assertEqual(repaired["material_groups"], payload["material_groups"])

    def test_different_source_conflict_is_not_automatically_repairable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            self.write_candidate(root, target, "exact_per_target", payload)
            conflict = copy.deepcopy(payload)
            conflict["source_collection"] = "Different Authoring Source"
            conflict["material_groups"][0]["mesh_ids"] = [99]
            mirror = self.write_candidate(
                root, target, "exact_global_target", conflict
            )
            before = mirror.read_bytes()

            plan = atlas_manifest_mirror_repair_plan(target)
            self.assertEqual(plan["status"], "unrepairable")
            self.assertEqual(
                plan["reason_code"], "atlas_manifest_ownership_conflict"
            )
            with self.assertRaises(AtlasManifestResolutionError):
                repair_atlas_manifest_mirrors(target)
            self.assertEqual(mirror.read_bytes(), before)

    def test_copied_blend_and_same_name_foreign_scope_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, blend, payload = self.fixture(temporary)
            copied = dict(payload)
            copied["blend_file"] = str(blend.with_name("copied.blend"))
            copied_path = self.write_candidate(
                root,
                target,
                "exact_target_scope",
                copied,
                name=f"copied__{target.stem}.json",
            )

            other_root = Path(temporary) / "Other" / root.name
            other_root.mkdir(parents=True)
            foreign = dict(payload)
            foreign["spm"] = str(other_root / target.name)
            foreign_path = self.write_candidate(
                root,
                target,
                "exact_target_scope",
                foreign,
                name=f"foreign__{target.stem}.json",
            )

            resolution = resolve_atlas_manifests(
                target,
                expected_blend=blend,
            )

            self.assertEqual(resolution["selected"], [])
            self.assertEqual(
                {(row["path"], row["reason"]) for row in resolution["rejected"]},
                {
                    (str(copied_path.resolve()), "foreign_blend_identity"),
                    (str(foreign_path.resolve()), "different_target_spm"),
                },
            )

    def test_legacy_and_non_target_scope_records_are_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            selected = self.write_candidate(
                root,
                target,
                "exact_target_scope",
                payload,
            )
            legacy = dict(payload)
            legacy["material_groups"] = [{
                "material": "M_leaf_test",
                "material_id": 999,
            }]
            legacy_path = self.write_candidate(
                root,
                target,
                "legacy_material_manifest",
                legacy,
            )
            shadow_path = self.write_candidate(
                root,
                target,
                "exact_target_scope",
                legacy,
                name="scope-identity-only.json",
            )

            resolution = resolve_atlas_manifests(target)

            self.assertEqual(
                [row["path"] for row in resolution["selected"]],
                [str(selected.resolve())],
            )
            self.assertEqual(
                {(row["path"], row["kind"], row["reason"])
                 for row in resolution["shadowed"]},
                {
                    (
                        str(legacy_path.resolve()),
                        "legacy_material_manifest",
                        "diagnostic_only_legacy_shadow",
                    ),
                    (
                        str(shadow_path.resolve()),
                        "scope_identity_shadow",
                        "diagnostic_only_scope_identity_shadow",
                    ),
                },
            )

    def test_resolution_and_read_only_evidence_never_rewrite_manifests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, target, _blend, payload = self.fixture(temporary)
            path = self.write_candidate(
                root,
                target,
                "exact_per_target",
                payload,
                name="stable-receipt.json",
            )
            before = (path.read_bytes(), path.stat().st_mtime_ns)

            first = resolve_atlas_manifests(target)
            evidence = resolution_evidence(first)
            second = resolve_atlas_manifests(target)

            self.assertEqual(first, second)
            self.assertNotIn("payload", evidence["selected"][0])
            self.assertEqual(
                (path.read_bytes(), path.stat().st_mtime_ns),
                before,
            )


if __name__ == "__main__":
    unittest.main()
