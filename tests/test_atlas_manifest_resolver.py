import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from atlas_manifest_resolver import (
    AtlasManifestResolutionError,
    atlas_manifest_mirror_repair_plan,
    diagnose_manifest_generator_candidates,
    repair_atlas_manifest_mirrors,
    resolution_evidence,
    resolve_atlas_manifests,
    resolve_manifest_material_ownership,
)


class AtlasManifestResolverTests(unittest.TestCase):
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


class ClusterPairLegacyNameIdentityTests(unittest.TestCase):
    """A Cluster output's Atlas records may name its legacy unprefixed file.

    ``SK_<name>.spm`` and ``<name>.spm`` are one Cluster output under two
    names.  An Atlas export aimed at the legacy name used to leave the
    canonical target with zero operational records -- on 2026-08-04 that was
    18 of 242 production rows, each reported as "Atlas producer 영수증이
    없습니다" with no recovery.  The normalization receipt is what makes the
    two names one identity, so it is required, and the canonical name still
    outranks the legacy one.
    """

    FIXTURE = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "issue165_cluster_pair_legacy_name_records.json"
        ).read_text(encoding="utf-8")
    )

    def build(self, temporary, *, receipt="complete"):
        fixture = self.FIXTURE
        cluster = Path(temporary) / fixture["asset_folder"] / "cluster"
        cluster.mkdir(parents=True)
        canonical = cluster / fixture["canonical_spm"]
        legacy = cluster / fixture["legacy_spm"]
        canonical.write_bytes(b"cluster-spm")
        legacy.write_bytes(b"cluster-spm")
        blend = Path(temporary) / "atlas" / "M_sanitized.blend"
        blend.parent.mkdir(parents=True, exist_ok=True)
        blend.write_bytes(b"blend")

        if receipt != "absent":
            reports = cluster / "reports"
            reports.mkdir(exist_ok=True)
            body = {
                "receipt_kind": "cluster_spm_output_name_normalization",
                "schema_version": 2,
                "status": "complete",
                "pair_id": self.pair_id(canonical, legacy),
                "invariants": {
                    "after_content_equal": True,
                    "canonical_output_authoritative": True,
                    "source_unchanged_during_copy": True,
                },
                "paths": {
                    "canonical_output": str(canonical),
                    "legacy_unprefixed_input": str(legacy),
                },
            }
            if receipt == "incomplete":
                body["status"] = "in_progress"
            elif receipt == "unproven":
                body["invariants"]["after_content_equal"] = False
            elif receipt == "foreign_pair":
                body["pair_id"] = "0" * 64
            (reports / f"{canonical.stem}_cluster_spm_pair.json").write_text(
                json.dumps(body, sort_keys=True), encoding="utf-8"
            )
        return cluster, canonical, legacy, blend

    @staticmethod
    def pair_id(canonical, legacy):
        import hashlib

        keys = sorted(
            os.path.normcase(os.path.abspath(str(path))).casefold()
            for path in (canonical, legacy)
        )
        return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()

    def payload(self, spm, blend, material, scope_id):
        return {
            "atlas_manifest_schema_version": 1,
            "spm": str(spm),
            "blend_file": str(blend),
            "source_collection": material["material"],
            "export_scope_id": scope_id,
            "material_groups": [{
                "material": material["material"],
                "material_id": material["material_id"],
                "mesh_ids": list(material["mesh_ids"]),
            }],
        }

    def write(self, cluster, relative, payload):
        path = cluster / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def write_production_shape(self, cluster, canonical, legacy, blend):
        """Reproduce the folder that blocked the real Cluster push."""
        fixture = self.FIXTURE
        cluster_record = self.payload(
            legacy, blend, fixture["cluster_material"], fixture["cluster_scope_id"]
        )
        leaf_record = self.payload(
            legacy, blend, fixture["leaf_material"], fixture["leaf_scope_id"]
        )
        foreign_record = self.payload(
            cluster / fixture["foreign_spm"],
            blend,
            fixture["leaf_material"],
            fixture["leaf_scope_id"],
        )
        paths = {
            "per_target_legacy": self.write(
                cluster,
                Path(".atlas_leaf_speedtree_targets") / f"{legacy.stem}.json",
                leaf_record,
            ),
            "per_target_foreign": self.write(
                cluster,
                Path(".atlas_leaf_speedtree_targets")
                / f"{Path(fixture['foreign_spm']).stem}.json",
                foreign_record,
            ),
            "cluster_scope_legacy": self.write(
                cluster,
                Path(".atlas_leaf_speedtree_scopes")
                / f"{fixture['cluster_scope_id']}__{legacy.stem}.json",
                cluster_record,
            ),
            "leaf_scope_legacy": self.write(
                cluster,
                Path(".atlas_leaf_speedtree_scopes")
                / f"{fixture['leaf_scope_id']}__{legacy.stem}.json",
                leaf_record,
            ),
            # Last-writer-wins identity shadow, diagnostic only.
            "scope_shadow": self.write(
                cluster,
                Path(".atlas_leaf_speedtree_scopes")
                / f"{fixture['leaf_scope_id']}.json",
                foreign_record,
            ),
        }
        return paths

    def test_legacy_named_records_resolve_for_the_canonical_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster, canonical, legacy, blend = self.build(temporary)
            paths = self.write_production_shape(
                cluster, canonical, legacy, blend
            )

            resolution = resolve_atlas_manifests(canonical)

            self.assertEqual(
                {row["path"] for row in resolution["selected"]},
                {
                    str(paths["per_target_legacy"].resolve()),
                    str(paths["cluster_scope_legacy"].resolve()),
                    str(paths["leaf_scope_legacy"].resolve()),
                },
                "the canonical target's own records, written under its legacy "
                "name, must be operational",
            )
            self.assertTrue(all(
                row["identity_match"] == "cluster_spm_pair_legacy_name"
                for row in resolution["selected"]
            ))
            self.assertEqual(
                resolution["cluster_pair_identity"]["counterpart_spm"],
                str(legacy),
            )

    def test_a_genuinely_different_spm_in_the_same_folder_stays_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster, canonical, legacy, blend = self.build(temporary)
            paths = self.write_production_shape(
                cluster, canonical, legacy, blend
            )

            resolution = resolve_atlas_manifests(canonical)

            rejected = {
                row["path"]: row["reason"] for row in resolution["rejected"]
            }
            self.assertEqual(
                rejected.get(str(paths["per_target_foreign"].resolve())),
                "different_target_spm",
                "a sibling provider SPM is not this target under another name",
            )

    def test_the_identity_shadow_is_never_operational(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster, canonical, legacy, blend = self.build(temporary)
            paths = self.write_production_shape(
                cluster, canonical, legacy, blend
            )

            resolution = resolve_atlas_manifests(canonical)

            self.assertNotIn(
                str(paths["scope_shadow"].resolve()),
                {row["path"] for row in resolution["selected"]},
            )
            self.assertIn(
                str(paths["scope_shadow"].resolve()),
                {row["path"] for row in resolution["shadowed"]},
            )

    def test_a_legacy_scope_record_satisfies_the_target_scope_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster, canonical, legacy, blend = self.build(temporary)
            self.write_production_shape(cluster, canonical, legacy, blend)

            resolution = resolve_atlas_manifests(canonical)

            self.assertNotIn(
                "target_scope_candidate_missing",
                {row["reason"] for row in resolution["missing"]},
            )

    def test_without_a_pair_receipt_the_legacy_name_proves_nothing(self):
        for receipt in ("absent", "incomplete", "unproven", "foreign_pair"):
            with self.subTest(receipt=receipt):
                with tempfile.TemporaryDirectory() as temporary:
                    cluster, canonical, legacy, blend = self.build(
                        temporary, receipt=receipt
                    )
                    self.write_production_shape(
                        cluster, canonical, legacy, blend
                    )

                    resolution = resolve_atlas_manifests(canonical)

                    self.assertEqual(
                        resolution["selected"],
                        [],
                        "name shape alone must never adopt another file's "
                        "Atlas records",
                    )
                    self.assertNotIn("cluster_pair_identity", resolution)

    def test_a_canonical_named_record_supersedes_a_disagreeing_legacy_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster, canonical, legacy, blend = self.build(temporary)
            fixture = self.FIXTURE
            current = self.payload(
                canonical,
                blend,
                fixture["cluster_material"],
                fixture["cluster_scope_id"],
            )
            stale = self.payload(
                legacy,
                blend,
                {**fixture["cluster_material"], "mesh_ids": [99]},
                fixture["cluster_scope_id"],
            )
            current_path = self.write(
                cluster,
                Path(".atlas_leaf_speedtree_targets") / f"{canonical.stem}.json",
                current,
            )
            stale_path = self.write(
                cluster,
                Path(".atlas_leaf_speedtree_targets") / f"{legacy.stem}.json",
                stale,
            )

            resolution = resolve_atlas_manifests(canonical)

            self.assertEqual(
                [row["path"] for row in resolution["selected"]],
                [str(current_path.resolve())],
            )
            superseded = {
                row["path"]: row["reason"] for row in resolution["shadowed"]
            }
            self.assertEqual(
                superseded.get(str(stale_path.resolve())),
                "superseded_legacy_name_record",
                "a rename artifact must not veto the production identity",
            )
            self.assertEqual(resolution["conflicting"], [])

    def test_two_canonical_named_records_that_disagree_still_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster, canonical, legacy, blend = self.build(temporary)
            fixture = self.FIXTURE
            first = self.payload(
                canonical,
                blend,
                fixture["cluster_material"],
                fixture["cluster_scope_id"],
            )
            second = self.payload(
                canonical,
                blend,
                {**fixture["cluster_material"], "mesh_ids": [99]},
                fixture["cluster_scope_id"],
            )
            self.write(
                cluster,
                Path(".atlas_leaf_speedtree_targets") / f"{canonical.stem}.json",
                first,
            )
            self.write(
                cluster,
                Path(".atlas_leaf_speedtree_scopes")
                / f"{fixture['cluster_scope_id']}__{canonical.stem}.json",
                second,
            )

            with self.assertRaises(AtlasManifestResolutionError) as caught:
                resolve_atlas_manifests(canonical)

            self.assertEqual(
                {
                    row["reason"]
                    for row in caught.exception.resolution["conflicting"]
                },
                {"operational_candidate_disagreement"},
            )

    def test_the_legacy_member_resolves_the_same_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster, canonical, legacy, blend = self.build(temporary)
            fixture = self.FIXTURE
            current_path = self.write(
                cluster,
                Path(".atlas_leaf_speedtree_targets") / f"{canonical.stem}.json",
                self.payload(
                    canonical,
                    blend,
                    fixture["cluster_material"],
                    fixture["cluster_scope_id"],
                ),
            )

            resolution = resolve_atlas_manifests(legacy)

            self.assertEqual(
                [row["path"] for row in resolution["selected"]],
                [str(current_path.resolve())],
                "querying the legacy member must see the canonical record",
            )

    def test_resolution_stays_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            cluster, canonical, legacy, blend = self.build(temporary)
            paths = self.write_production_shape(
                cluster, canonical, legacy, blend
            )
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in paths.values()
            }

            resolve_atlas_manifests(canonical)

            self.assertEqual(
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in paths.values()
                },
                before,
            )


if __name__ == "__main__":
    unittest.main()
