import json
import tempfile
import unittest
from pathlib import Path

from atlas_manifest_resolver import (
    AtlasManifestResolutionError,
    resolution_evidence,
    resolve_atlas_manifests,
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
