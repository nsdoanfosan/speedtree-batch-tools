import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
if str(SK_BATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SK_BATCH_DIR))
BATCH_TOOLS_DIR = SK_BATCH_DIR.parent
if str(BATCH_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(BATCH_TOOLS_DIR))

from cluster_assembly_handoff_contract import (  # noqa: E402
    _compare_artifact,
    assembly_source_fbx_from_contract,
    assembly_source_fbx_resolution,
    build_assembly_handoff,
    build_blender_fbx_inventory,
    classify_inventory_role,
    file_fingerprint,
    normalize_export_name,
    resolve_cluster_receipt_path,
    role_identity_aliases_from_contract,
)
from pcg_st9_texture_batch.pcg_cluster_assembly_contract import (  # noqa: E402
    ClusterAssemblyReceiptAmbiguityError,
)


class FakeMaterial:
    def __init__(self, name):
        self.name = name


class FakeVertex:
    def __init__(self, co):
        self.co = co


class FakePolygon:
    def __init__(self, index, material_index, vertices):
        self.index = index
        self.material_index = material_index
        self.vertices = vertices


class FakeMesh:
    def __init__(self, name, materials, polygons, vertex_count=32):
        self.name = name
        self.materials = [FakeMaterial(value) if value else None for value in materials]
        self.polygons = polygons
        self.vertices = [FakeVertex((index, index % 3, 0.0)) for index in range(vertex_count)]


class FakeObject(dict):
    type = "MESH"

    def __init__(self, name, mesh, source_fbx):
        super().__init__(codex_source_fbx=str(source_fbx))
        self.name = name
        self.data = mesh
        self.matrix_world = (
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )


def role_object(source_fbx, *, material="M_branch_elm_01", used=True, name="TreeMesh"):
    polygons = [
        FakePolygon(0, 0 if used else 1, (0, 1, 2)),
        FakePolygon(1, 0 if used else 1, (2, 3, 0)),
        FakePolygon(2, 0 if used else 1, (10, 11, 12)),
    ]
    materials = [material, "M_bark_elm_01"]
    return FakeObject(name, FakeMesh(name + "Data", materials, polygons), source_fbx)


def write_receipt(
    path,
    spm,
    fbx,
    roles,
    *,
    fingerprint=None,
    assembly_spm=None,
    normalized_by_role=None,
    target_material_by_role=None,
):
    fingerprint = dict(fingerprint or file_fingerprint(fbx))
    assembly_spm = Path(assembly_spm or spm)
    contract_roles = []
    for role, identity, decision in roles:
        normalized = (normalized_by_role or {}).get(role)
        if (
            isinstance(normalized, dict)
            and normalized.get("status") == "ready"
            and normalized.get("variants")
            and not normalized.get("delivery_mode")
        ):
            normalized = dict(normalized)
            binding = {
                "generator_index": 0,
                "slot_prefix": "Material:Frond:0",
                "target_material_id": 1,
                "target_mesh_id": 1,
            }
            normalized.update({
                "delivery_mode": "render_connected",
                "generator_bindings": [binding],
                "target_deliveries": [{
                    "schema_version": 1,
                    "spm": str(assembly_spm),
                    "delivery_mode": "render_connected",
                    "delivery_decision": "normalize_part",
                    "delivery_reason":
                        "generator_connection_matches_live_export",
                    "generator_bindings": [binding],
                    "missing_live_bindings": [],
                    "binding_mismatches": [],
                }],
            })
        target = {
            "spm": str(assembly_spm),
            "export_bundle": {"fbx": fingerprint},
            "fbx_material_mesh_pair": {"decision": decision},
        }
        target_material = (target_material_by_role or {}).get(role)
        if target_material:
            target["spm_material_mesh_pair"] = {
                "status": "complete_pair",
                "records": [{
                    "material_name": target_material,
                    "complete": True,
                }],
            }
        contract_roles.append({
            "role": role,
            "name": identity,
            "decision": decision,
            "spm": str(spm),
            "normalized_variants": normalized,
            "targets": [target],
        })
    payload = {
        "items": [{
            "spm": str(spm),
            "cluster_assembly": {
                "schema_version": 1,
                "folder": str(spm.parent / "Tree_elm"),
                "tree_source_identities": [{
                    "target_spm": file_fingerprint(spm),
                    "authoritative_tree_source": file_fingerprint(assembly_spm),
                }],
                "dependencies": [],
                "handoff": {"status": "pending_export", "roles": contract_roles},
            },
        }],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class ClusterAssemblyHandoffTests(unittest.TestCase):
    def test_matching_content_hash_allows_mtime_only_drift(self):
        expected = {
            "path": r"D:\Trees\SK_Tree_elm_01.spm",
            "exists": True,
            "size": 377169,
            "mtime_ns": 100,
            "sha256": "same-content",
        }
        actual = {
            **expected,
            "mtime_ns": 200,
        }

        validation = _compare_artifact(expected, actual)

        self.assertTrue(validation["ok"])
        self.assertEqual(
            validation["status"],
            "content_exact_metadata_drift",
        )
        self.assertEqual(validation["metadata_drift"], ["mtime_ns"])

    def test_content_hash_mismatch_is_not_hidden_by_matching_metadata(self):
        expected = {
            "path": r"D:\Trees\SK_Tree_elm_01.spm",
            "exists": True,
            "size": 377169,
            "mtime_ns": 100,
            "sha256": "old-content",
        }
        actual = {
            **expected,
            "sha256": "new-content",
        }

        validation = _compare_artifact(expected, actual)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["status"], "sha256_mismatch")

    def test_metadata_only_receipt_still_rejects_mtime_drift(self):
        expected = {
            "path": r"D:\Trees\SK_Tree_elm_01.spm",
            "exists": True,
            "size": 377169,
            "mtime_ns": 100,
        }
        actual = {
            **expected,
            "mtime_ns": 200,
        }

        validation = _compare_artifact(expected, actual)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["status"], "mtime_ns_mismatch")

    def test_persisted_cluster_receipt_is_preferred_without_a_new_batch_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            embedded = root / "material_contract.json"
            persisted = root / "persisted_cluster_receipt.json"
            fbx = root / "Tree_elm_01.fbx"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                embedded,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pending_export")],
            )
            persisted.write_text("{}", encoding="utf-8")
            with mock.patch(
                "pcg_st9_texture_batch.pcg_cluster_assembly_contract."
                "locate_cluster_assembly_receipt",
                return_value=persisted,
            ):
                resolved = resolve_cluster_receipt_path(spm, embedded)
            self.assertEqual(resolved, persisted.resolve())

    def test_run_specific_live_audit_overrides_hash_current_persisted_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            embedded = root / "material_contract.json"
            persisted = root / "persisted_cluster_receipt.json"
            fbx = root / "Tree_elm_01.fbx"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                embedded,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pass_through")],
            )
            payload = json.loads(embedded.read_text(encoding="utf-8"))
            payload["cluster_assembly_receipt_persistence"] = {
                "status": "ok",
                "stage": "receipt_persistence",
                "live_audit_complete": True,
            }
            embedded.write_text(json.dumps(payload), encoding="utf-8")
            persisted.write_text("{}", encoding="utf-8")

            with mock.patch(
                "pcg_st9_texture_batch.pcg_cluster_assembly_contract."
                "cluster_assembly_receipt_resolution",
                return_value={"selected_receipt": str(persisted)},
            ) as persisted_resolution:
                resolved, resolution = resolve_cluster_receipt_path(
                    spm,
                    embedded,
                    include_resolution=True,
                )

            self.assertEqual(resolved, embedded.resolve())
            self.assertEqual(
                resolution["policy"],
                "embedded_live_audit_authoritative",
            )
            persisted_resolution.assert_not_called()

    def test_divergent_persisted_receipts_fail_closed_without_live_audit(self):
        spm = Path("C:/Trees/SK_Tree_elm_01.spm")
        with mock.patch(
            "pcg_st9_texture_batch.pcg_cluster_assembly_contract."
            "locate_cluster_assembly_receipt",
            side_effect=ClusterAssemblyReceiptAmbiguityError(
                "divergent current receipts"
            ),
        ):
            with self.assertRaises(ClusterAssemblyReceiptAmbiguityError):
                resolve_cluster_receipt_path(spm)

    def test_unmarked_embedded_contract_is_not_missing_receipt_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            embedded = root / "material_contract.json"
            fbx = root / "Tree_elm_01.fbx"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                embedded,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pending_export")],
            )
            with mock.patch(
                "pcg_st9_texture_batch.pcg_cluster_assembly_contract."
                "cluster_assembly_receipt_resolution",
                side_effect=FileNotFoundError,
            ):
                resolved, resolution = resolve_cluster_receipt_path(
                    spm,
                    embedded,
                    include_resolution=True,
                )
            self.assertIsNone(resolved)
            self.assertEqual(
                resolution["policy"],
                "no_cluster_assembly_receipt",
            )
            self.assertIsNone(resolution["selected_receipt"])

    def test_unmarked_embedded_contract_is_not_stale_receipt_authority(self):
        from pcg_st9_texture_batch.pcg_cluster_assembly_contract import (
            ClusterAssemblyReceiptStaleError,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            embedded = root / "live_audit_contract.json"
            fbx = root / "Tree_elm_01.fbx"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                embedded,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pending_export")],
            )
            with mock.patch(
                "pcg_st9_texture_batch.pcg_cluster_assembly_contract."
                "cluster_assembly_receipt_resolution",
                side_effect=ClusterAssemblyReceiptStaleError("old snapshot"),
            ):
                resolved, resolution = resolve_cluster_receipt_path(
                    spm,
                    embedded,
                    include_resolution=True,
                )
            self.assertIsNone(resolved)
            self.assertEqual(
                resolution["policy"],
                "no_cluster_assembly_receipt",
            )
            self.assertIn(
                "old snapshot",
                resolution["ignored_stale_candidates"][0]["error"],
            )

    def test_export_name_normalization_keeps_role_identity(self):
        self.assertEqual(
            normalize_export_name("Material::M_branch_elm_01_Mat"),
            "branch_elm_01",
        )

    def test_leaf_side_is_an_independent_role(self):
        from cluster_assembly_handoff_contract import dependency_role

        self.assertEqual(dependency_role("SK_leaf_elm_side_01"), "leaf_side")

    def test_generic_cluster_is_an_independent_role(self):
        from cluster_assembly_handoff_contract import dependency_role

        self.assertEqual(
            dependency_role("SK_cluster_densiflora_01"),
            "cluster",
        )

    def test_actual_polygon_assignment_is_complete_and_componentized(self):
        with tempfile.TemporaryDirectory() as tmp:
            fbx = Path(tmp) / "tree.fbx"
            fbx.write_bytes(b"fbx")
            inventory = build_blender_fbx_inventory(
                [role_object(fbx)],
                fbx,
                {"branch": "branch_elm_01", "leaf": "leaf_elm_01"},
            )
            role = classify_inventory_role(inventory, "branch", "branch_elm_01")
            self.assertEqual(role["status"], "complete_pair")
            self.assertEqual(role["decision"], "normalize_part")
            self.assertEqual(role["assignments"][0]["used_polygon_count"], 3)
            self.assertEqual(len(role["assignments"][0]["components"]), 2)

    def test_assembly_source_fbx_comes_from_receipt_not_full_sk_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full_spm = root / "SK_Tree_elm_01.spm"
            source_spm = root / "Tree_elm_01.spm"
            source_fbx = root / "fbx" / "Tree_elm_01.fbx"
            receipt = root / "pcg_receipt.json"
            full_spm.write_bytes(b"full")
            source_spm.write_bytes(b"source")
            source_fbx.parent.mkdir()
            source_fbx.write_bytes(b"source fbx")
            write_receipt(
                receipt,
                full_spm,
                source_fbx,
                [
                    ("branch", "branch_elm_01", "normalize_part"),
                    ("leaf", "leaf_elm_01", "normalize_part"),
                ],
                assembly_spm=source_spm,
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            contract = payload["items"][0]["cluster_assembly"]
            selected = assembly_source_fbx_from_contract(contract, full_spm)
            self.assertEqual(selected.resolve(), source_fbx.resolve())
            self.assertNotEqual(selected.name, "SK_Tree_elm_01.fbx")

    def test_full_sk_identity_selects_one_source_from_multi_target_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full_spm = root / "SK_Tree_elm_01.spm"
            source_spm = root / "Tree_elm_01.spm"
            other_spm = root / "Tree_elm_02.spm"
            source_fbx = root / "fbx" / "Tree_elm_01.fbx"
            other_fbx = root / "fbx" / "Tree_elm_02.fbx"
            for path, data in (
                (full_spm, b"full"),
                (source_spm, b"source"),
                (other_spm, b"other"),
                (source_fbx, b"source fbx"),
                (other_fbx, b"other fbx"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            contract = {
                "tree_source_identities": [{
                    "target_spm": file_fingerprint(full_spm),
                    "authoritative_tree_source": file_fingerprint(source_spm),
                }],
                "handoff": {
                    "roles": [{
                        "role": "branch",
                        "name": "branch_elm_01",
                        "targets": [
                            {
                                "spm": str(source_spm),
                                "export_bundle": {
                                    "fbx": file_fingerprint(source_fbx)
                                },
                            },
                            {
                                "spm": str(other_spm),
                                "export_bundle": {
                                    "fbx": file_fingerprint(other_fbx)
                                },
                            },
                        ],
                    }],
                },
            }
            selected = assembly_source_fbx_from_contract(contract, full_spm)
            self.assertEqual(selected.resolve(), source_fbx.resolve())

    def test_contract_without_cluster_roles_needs_no_assembly_source_fbx(self):
        contract = {
            "folder": r"D:\Trees\Tree_plain",
            "dependencies": [],
            "handoff": {"status": "pass_through", "roles": []},
        }
        self.assertIsNone(
            assembly_source_fbx_from_contract(
                contract, r"D:\Trees\Tree_plain\SK_Tree_plain_01.spm"
            )
        )

    def test_pending_missing_assembly_fbx_is_recorded_legacy_pass_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full_spm = root / "SK_Tree_elm_02.spm"
            source_spm = root / "Tree_elm_02.spm"
            missing_fbx = root / "fbx" / "Tree_elm_02.fbx"
            receipt = root / "pcg_receipt.json"
            full_spm.write_bytes(b"full")
            source_spm.write_bytes(b"source")
            write_receipt(
                receipt,
                full_spm,
                missing_fbx,
                [
                    ("branch", "branch_elm_01", "pending_export"),
                    ("leaf", "leaf_elm_01", "pending_export"),
                ],
                assembly_spm=source_spm,
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            contract = payload["items"][0]["cluster_assembly"]

            resolution = assembly_source_fbx_resolution(contract, full_spm)

            self.assertEqual(resolution["status"], "legacy_pass_through")
            self.assertEqual(
                resolution["reason"], "assembly_source_fbx_pending_export"
            )
            self.assertEqual(Path(resolution["source_spm"]), source_spm)
            self.assertEqual(Path(resolution["source_fbx"]), missing_fbx)
            self.assertIsNone(
                assembly_source_fbx_from_contract(contract, full_spm)
            )

    def test_declared_but_unused_material_is_blocked_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            fbx = Path(tmp) / "tree.fbx"
            fbx.write_bytes(b"fbx")
            inventory = build_blender_fbx_inventory(
                [role_object(fbx, used=False)],
                fbx,
                {"branch": "branch_elm_01"},
            )
            role = classify_inventory_role(inventory, "branch", "branch_elm_01")
            self.assertEqual(role["status"], "material_without_mesh")
            self.assertEqual(role["decision"], "blocked")

    def test_role_named_mesh_without_material_is_blocked_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            fbx = Path(tmp) / "tree.fbx"
            fbx.write_bytes(b"fbx")
            obj = role_object(
                fbx,
                material="M_bark_elm_01",
                name="Model::branch_elm_01",
            )
            inventory = build_blender_fbx_inventory(
                [obj], fbx, {"branch": "branch_elm_01"}
            )
            role = classify_inventory_role(inventory, "branch", "branch_elm_01")
            self.assertEqual(role["status"], "mesh_without_material")
            self.assertEqual(role["decision"], "blocked")

    def test_both_absent_is_legacy_pass_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            fbx = Path(tmp) / "tree.fbx"
            fbx.write_bytes(b"fbx")
            inventory = build_blender_fbx_inventory(
                [role_object(fbx, material="M_bark_elm_01")],
                fbx,
                {"branch": "branch_elm_01"},
            )
            role = classify_inventory_role(inventory, "branch", "branch_elm_01")
            self.assertEqual(role["status"], "absent")
            self.assertEqual(role["decision"], "pass_through")

    def test_undeclared_generic_leaf_material_is_not_an_assembly_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_weed_velvet_grass_03.spm"
            fbx = root / "fbx" / "SK_weed_velvet_grass_03.fbx"
            receipt = root / "pcg_receipt.json"
            spm.write_bytes(b"spm")
            fbx.parent.mkdir()
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("cluster", "cluster_velvet_grass_01", "pass_through")],
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            contract = payload["items"][0]["cluster_assembly"]
            identities = role_identity_aliases_from_contract(contract, spm)
            inventory = build_blender_fbx_inventory(
                [
                    role_object(
                        fbx,
                        material="M_leaf_velvet_grass_01",
                    )
                ],
                fbx,
                identities,
            )

            handoff = build_assembly_handoff(receipt, spm, inventory)

            leaf = next(
                row for row in handoff["roles"] if row["role"] == "leaf"
            )
            self.assertEqual(identities["leaf"], [])
            self.assertEqual(leaf["status"], "absent")
            self.assertEqual(leaf["decision"], "pass_through")
            self.assertEqual(
                leaf["reconciliation"],
                "receipt_and_fbx_absent",
            )
            self.assertEqual(handoff["status"], "pass_through")

    def test_complete_branch_and_absent_leaf_build_separate_assembly_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            fbx = root / "fbx" / "SK_Tree_elm_01.fbx"
            receipt = root / "pcg_receipt.json"
            spm.write_bytes(b"spm")
            fbx.parent.mkdir()
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [
                    ("branch", "branch_elm_01", "pending_export"),
                    ("leaf", "leaf_elm_01", "pass_through"),
                ],
                normalized_by_role={
                    "branch": {
                        "status": "ready",
                        "material": "M_branch_elm_01",
                        "variants": [{
                            "ordinal": 1,
                            "plan_name": "branch_elm_01_01",
                            "skeletal_asset_name": "SK_branch_elm_01_01",
                            "source_prototype_index": 1,
                            "source_partition_mode": "PER_DEFORM_ROOT",
                        }],
                    }
                },
            )
            inventory = build_blender_fbx_inventory(
                [role_object(fbx)],
                fbx,
                {"branch": "branch_elm_01", "leaf": "leaf_elm_01"},
            )
            handoff = build_assembly_handoff(receipt, spm, inventory)
            self.assertEqual(handoff["status"], "ready")
            self.assertTrue(handoff["full_skeletal_mesh"]["preserved"])
            self.assertTrue(handoff["assembly"]["requested"])
            self.assertEqual(
                [row["role"] for row in handoff["assembly"]["part_builder_inputs"]],
                ["branch"],
            )
            self.assertEqual(
                handoff["assembly"]["part_builder_inputs"][0]["role_identity"],
                "M_branch_elm_01",
            )
            self.assertEqual(
                handoff["assembly"]["part_builder_inputs"][0][
                    "normalized_variants"
                ]["variants"][0]["source_prototype_index"],
                1,
            )
            self.assertEqual(
                handoff["assembly"]["part_builder_inputs"][0][
                    "normalized_variants"
                ]["variants"][0]["skeletal_asset_name"],
                "SK_branch_elm_01_01",
            )
            self.assertFalse(
                handoff["skeleton_wind_contract"]["production_310_bone_hard_gate"]
            )

    def test_complete_target_spm_material_is_a_content_driven_role_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_tree_provider_current_01.spm"
            assembly_spm = root / "tree_provider_current_01.spm"
            fbx = root / "tree_provider_current_01.fbx"
            receipt = root / "receipt.json"
            spm.write_bytes(b"sk")
            assembly_spm.write_bytes(b"authoritative")
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("leaf", "M_leaf_provider_legacy_01", "pending_export")],
                assembly_spm=assembly_spm,
                normalized_by_role={
                    "leaf": {
                        "status": "ready",
                        "material": "M_leaf_provider_legacy_01",
                        "variants": [{
                            "ordinal": 1,
                            "source_partition_mode": "PER_DEFORM_ROOT",
                        }],
                    },
                },
                target_material_by_role={
                    "leaf": "leaf_provider_current_01",
                },
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            contract = payload["items"][0]["cluster_assembly"]
            identities = role_identity_aliases_from_contract(contract, spm)
            inventory = build_blender_fbx_inventory(
                [
                    role_object(
                        fbx,
                        material="leaf_provider_current_01_Mat",
                    )
                ],
                fbx,
                identities,
            )

            handoff = build_assembly_handoff(receipt, spm, inventory)

            self.assertEqual(
                identities["leaf"],
                ["leaf_provider_current_01", "M_leaf_provider_legacy_01"],
            )
            self.assertEqual(handoff["status"], "ready")
            leaf = next(
                row for row in handoff["roles"] if row["role"] == "leaf"
            )
            self.assertEqual(
                leaf["role_identity"],
                "leaf_provider_current_01",
            )
            self.assertEqual(leaf["status"], "complete_pair")
            self.assertEqual(
                leaf["role_identity_aliases"],
                ["M_leaf_provider_legacy_01"],
            )
            self.assertEqual(
                handoff["assembly"]["part_builder_inputs"][0][
                    "role_identity_aliases"
                ],
                ["M_leaf_provider_legacy_01"],
            )

    def test_actionable_role_without_normalized_variants_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            fbx = root / "tree.fbx"
            receipt = root / "receipt.json"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pending_export")],
            )
            inventory = build_blender_fbx_inventory(
                [role_object(fbx)], fbx, {"branch": "branch_elm_01"}
            )
            handoff = build_assembly_handoff(receipt, spm, inventory)
            self.assertEqual(handoff["status"], "blocked")
            self.assertIn(
                "normalized_variants_required",
                [row.get("reason") for row in handoff["issues"]],
            )

    def test_asset_registration_only_is_pass_through_before_bwr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            fbx = root / "tree.fbx"
            receipt = root / "receipt.json"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pending_export")],
                normalized_by_role={
                    "branch": {
                        "status": "ready",
                        "variants": [{"ordinal": 1}],
                        "delivery_mode": "asset_registration_only",
                        "target_deliveries": [{
                            "schema_version": 1,
                            "spm": str(spm),
                            "delivery_mode":
                                "asset_registration_only",
                            "generator_bindings": [],
                        }],
                    },
                },
            )
            inventory = build_blender_fbx_inventory(
                [role_object(fbx)], fbx, {"branch": "branch_elm_01"}
            )

            handoff = build_assembly_handoff(receipt, spm, inventory)

            branch = next(
                row for row in handoff["roles"]
                if row["role"] == "branch"
            )
            self.assertEqual(branch["decision"], "pass_through")
            self.assertEqual(
                branch["reconciliation"],
                "asset_registration_only",
            )

    def test_incomplete_generator_connection_is_blocked_before_bwr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            fbx = root / "tree.fbx"
            receipt = root / "receipt.json"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pending_export")],
                normalized_by_role={
                    "branch": {
                        "status": "ready",
                        "variants": [{"ordinal": 1}],
                        "delivery_mode": "connection_incomplete",
                        "target_deliveries": [{
                            "schema_version": 1,
                            "spm": str(spm),
                            "delivery_mode": "connection_incomplete",
                            "generator_bindings": [],
                        }],
                    },
                },
            )
            inventory = build_blender_fbx_inventory(
                [role_object(fbx)], fbx, {"branch": "branch_elm_01"}
            )

            handoff = build_assembly_handoff(receipt, spm, inventory)

            self.assertEqual(handoff["status"], "blocked")
            branch = next(
                row for row in handoff["roles"]
                if row["role"] == "branch"
            )
            self.assertEqual(branch["decision"], "blocked")
            self.assertEqual(
                branch["reconciliation"],
                "generator_connection_contract_incomplete",
            )

    def test_bark_block_identifies_exact_provider_and_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            provider = root / "Cluster" / "SK_leaf_elm_01.spm"
            fbx = root / "tree.fbx"
            receipt = root / "receipt.json"
            spm.write_bytes(b"spm")
            provider.parent.mkdir()
            provider.write_bytes(b"provider")
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("leaf", "leaf_elm_01", "pending_export")],
                normalized_by_role={
                    "leaf": {
                        "status": "ready",
                        "variants": [{"ordinal": 1}],
                    },
                },
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            handoff_contract = payload["items"][0][
                "cluster_assembly"
            ]["handoff"]
            handoff_contract["status"] = "needs_bark_normalization"
            handoff_contract["cluster_dependencies"] = [{
                "role": "leaf",
                "spm": str(provider),
                "output_spm": str(provider),
            }]
            handoff_contract["canonical_bark"] = {
                "status": "replacement_required",
                "mutation_requested": True,
                "canonical_material": "M_bark_elm_01",
                "cluster_bark_sources": [{
                    "cluster_spm": str(provider),
                    "material_name": "M_bark_common_end_01",
                    "replacement": "required",
                    "texture_refs": ["T_bark_common_end_01_color.tga"],
                }],
            }
            receipt.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            inventory = build_blender_fbx_inventory(
                [
                    role_object(
                        fbx,
                        material="M_leaf_elm_01",
                    )
                ],
                fbx,
                {"leaf": "leaf_elm_01"},
            )

            handoff = build_assembly_handoff(receipt, spm, inventory)

            self.assertEqual(handoff["status"], "blocked")
            issue = next(
                row
                for row in handoff["issues"]
                if row["code"]
                == "CANONICAL_BARK_NORMALIZATION_REQUIRED"
            )
            self.assertEqual(issue["role"], "leaf")
            self.assertEqual(issue["spm"], str(provider))
            self.assertEqual(
                issue["material"],
                "M_bark_common_end_01",
            )
            self.assertEqual(
                issue["canonical_material"],
                "M_bark_elm_01",
            )

    def test_legacy_ambiguous_bark_audit_does_not_block_live_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_tree_black_locast_01.spm"
            fbx = root / "tree.fbx"
            receipt = root / "receipt.json"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("branch", "branch_black_locast_01", "pending_export")],
                normalized_by_role={
                    "branch": {
                        "status": "ready",
                        "variants": [{"ordinal": 1}],
                    },
                },
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            handoff_contract = payload["items"][0][
                "cluster_assembly"
            ]["handoff"]
            conflicts = [
                {
                    "material_identity": "bark_black_locast_01",
                    "texture_basenames": ["uktladjcw_4k_albedo.tif"],
                    "providers": [str(root / "SK_branch_black_locast_01.spm")],
                },
                {
                    "material_identity": "bark_black_locast_01",
                    "texture_basenames": [
                        "t_bark_black_locast_01_color.tga"
                    ],
                    "providers": [str(root / "SK_cluster_black_locast_01.spm")],
                },
            ]
            handoff_contract["status"] = "blocked"
            handoff_contract["canonical_bark"] = {
                "status": "blocked_canonical_ambiguous",
                "canonical_material": "M_bark_black_locast_01",
                "canonical_conflicts": conflicts,
            }
            receipt.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            inventory = build_blender_fbx_inventory(
                [
                    role_object(
                        fbx,
                        material="M_branch_black_locast_01",
                    )
                ],
                fbx,
                {"branch": "branch_black_locast_01"},
            )

            handoff = build_assembly_handoff(receipt, spm, inventory)

            self.assertNotEqual(handoff["status"], "blocked", handoff)
            self.assertFalse(any(
                row.get("code") == "CANONICAL_BARK_AMBIGUOUS"
                for row in handoff["issues"]
            ))
            self.assertEqual(
                handoff["canonical_bark_delivery"],
                {
                    "status": "blocked_canonical_ambiguous",
                    "mutation_requested": False,
                    "normalization_gate_applied": False,
                },
            )

    def test_exact_isolated_bark_capture_satisfies_stale_receipt_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            provider = root / "Cluster" / "SK_leaf_elm_01.spm"
            isolated = root / "isolated" / "SK_leaf_elm_01.spm"
            manifest = root / "isolated" / "manifest.json"
            fbx = root / "tree.fbx"
            receipt = root / "receipt.json"
            spm.write_bytes(b"spm")
            provider.parent.mkdir()
            provider.write_bytes(b"provider")
            isolated.parent.mkdir()
            isolated.write_bytes(b"isolated provider")
            manifest.write_text("{}", encoding="utf-8")
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("leaf", "leaf_elm_01", "pending_export")],
                normalized_by_role={
                    "leaf": {
                        "status": "ready",
                        "variants": [{"ordinal": 1}],
                    },
                },
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            handoff_contract = payload["items"][0][
                "cluster_assembly"
            ]["handoff"]
            handoff_contract["status"] = "needs_bark_normalization"
            handoff_contract["cluster_dependencies"] = [{
                "role": "leaf",
                "spm": str(provider),
                "output_spm": str(provider),
                "spm_fingerprint": file_fingerprint(provider),
            }]
            handoff_contract["canonical_bark"] = {
                "status": "replacement_required",
                "mutation_requested": True,
                "canonical_material": "M_bark_elm_01",
                "cluster_bark_sources": [{
                    "cluster_spm": str(provider),
                    "material_name": "M_bark_common_end_01",
                    "replacement": "required",
                    "texture_refs": ["T_bark_common_end_01_color.tga"],
                }],
            }
            receipt.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            reports = provider.parent / "reports"
            reports.mkdir()
            source_fingerprint = file_fingerprint(provider)
            isolated_fingerprint = file_fingerprint(isolated)
            report = {
                "cluster_bark_source_resolution": {
                    "status": "ready",
                    "production_source_mutated": False,
                    "canonical_material": "M_bark_elm_01",
                    "source_spm": source_fingerprint,
                    "speedtree_spm": isolated_fingerprint,
                    "manifest": file_fingerprint(manifest),
                },
                "speedtree_material_handoff_contract": {
                    "outcome": "ok",
                    "source": {
                        "spm": {
                            **isolated_fingerprint,
                            "canonical_path": str(isolated),
                        },
                    },
                },
            }
            (
                reports
                / "SK_leaf_elm_01_speedtree_repair_pipeline_report_codex.json"
            ).write_text(json.dumps(report), encoding="utf-8")
            inventory = build_blender_fbx_inventory(
                [role_object(fbx, material="M_leaf_elm_01")],
                fbx,
                {"leaf": "leaf_elm_01"},
            )

            handoff = build_assembly_handoff(receipt, spm, inventory)

            self.assertNotEqual(handoff["status"], "blocked", handoff)
            self.assertEqual(
                handoff["canonical_bark_captures"][0]["status"],
                "ready",
            )
            self.assertFalse(any(
                row.get("code")
                == "CANONICAL_BARK_NORMALIZATION_REQUIRED"
                for row in handoff["issues"]
            ))

    def test_receipt_pass_through_disagreeing_with_real_pair_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            fbx = root / "SK_Tree_elm_01.fbx"
            receipt = root / "pcg_receipt.json"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"fbx")
            write_receipt(
                receipt,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pass_through")],
            )
            inventory = build_blender_fbx_inventory(
                [role_object(fbx)], fbx, {"branch": "branch_elm_01"}
            )
            handoff = build_assembly_handoff(receipt, spm, inventory)
            self.assertEqual(handoff["status"], "blocked")
            self.assertIn(
                "pcg_receipt_fbx_decision_mismatch",
                [row.get("reason") for row in handoff["issues"]],
            )

    def test_stale_fbx_receipt_fingerprint_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            fbx = root / "SK_Tree_elm_01.fbx"
            receipt = root / "pcg_receipt.json"
            spm.write_bytes(b"spm")
            fbx.write_bytes(b"current fbx")
            stale = dict(file_fingerprint(fbx))
            stale["size"] += 1
            stale["sha256"] = "stale-content"
            write_receipt(
                receipt,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pending_export")],
                fingerprint=stale,
            )
            inventory = build_blender_fbx_inventory(
                [role_object(fbx)], fbx, {"branch": "branch_elm_01"}
            )
            handoff = build_assembly_handoff(receipt, spm, inventory)
            self.assertEqual(handoff["status"], "blocked")
            self.assertIn(
                "CLUSTER_EXPORT_ARTIFACT_MISMATCH",
                [row["code"] for row in handoff["issues"]],
            )

    def test_post_export_handoff_rejects_replaced_xml_and_stmat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spm = root / "SK_Tree_elm_01.spm"
            fbx = root / "fbx" / "SK_Tree_elm_01.fbx"
            xml = root / "xml" / "SK_Tree_elm_01.xml"
            stmat = fbx.with_suffix(".stmat")
            receipt = root / "pcg_receipt.json"
            spm.write_bytes(b"spm")
            for path, content in (
                (fbx, b"fbx"),
                (xml, b"<old />"),
                (stmat, b"<old-materials />"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            write_receipt(
                receipt,
                spm,
                fbx,
                [("branch", "branch_elm_01", "pending_export")],
            )
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            bundle = payload["items"][0]["cluster_assembly"]["handoff"][
                "roles"
            ][0]["targets"][0]["export_bundle"]
            bundle["xml"] = file_fingerprint(xml)
            bundle["stmat"] = file_fingerprint(stmat)
            receipt.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            xml.write_bytes(b"<new-and-different />")
            stmat.write_bytes(b"<new-and-different-materials />")
            inventory = build_blender_fbx_inventory(
                [role_object(fbx)],
                fbx,
                {"branch": "branch_elm_01"},
            )

            handoff = build_assembly_handoff(receipt, spm, inventory)
            validations = {
                row["artifact"]: row
                for row in handoff["artifact_validation"]
            }

            self.assertEqual(handoff["status"], "blocked")
            self.assertEqual(validations["xml"]["status"], "sha256_mismatch")
            self.assertEqual(
                validations["stmat"]["status"],
                "sha256_mismatch",
            )

    def test_inventory_excludes_objects_from_another_fbx(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = root / "expected.fbx"
            other = root / "other.fbx"
            expected.write_bytes(b"expected")
            other.write_bytes(b"other")
            inventory = build_blender_fbx_inventory(
                [role_object(other)], expected, {"branch": "branch_elm_01"}
            )
            self.assertEqual(inventory["objects"], [])


if __name__ == "__main__":
    unittest.main()
