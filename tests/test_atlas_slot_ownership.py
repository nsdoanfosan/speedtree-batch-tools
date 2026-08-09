import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import atlas_slot_ownership as ownership


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "issue163_arbitrary_slot_takeovers.json"
)


def _payload(target, provider):
    return {
        "spm": str(target),
        "blend_file": str(target.parent / provider["blend"]),
        "source_collection": provider["collection"],
        "export_scope_id": provider["scope"],
        "speedtree_material_groups": [
            {
                "material": provider["material"],
                "material_id": provider["material_id"],
                "mesh_ids": list(provider["mesh_ids"]),
            }
        ],
        "generator_connection": {
            "requested": True,
            "complete": True,
            "bindings": copy.deepcopy(provider["legacy_bindings"]),
        },
    }


def _snapshot(target, fixture, phase_name):
    phase = next(
        row for row in fixture["phases"] if row["name"] == phase_name
    )
    providers = {row["scope"]: row for row in fixture["providers"]}
    leaf_guid = fixture["leaf_generator_guid"]
    bindings = []
    for scope, rows in phase["owners"].items():
        provider = providers[scope]
        for row in rows:
            frond = row["generator_guid"] != leaf_guid
            bindings.append({
                "generator_guid": row["generator_guid"],
                "slot_prefix": row["slot_prefix"],
                "generator_index": 20 if frond else 2,
                "generator_name": "Frond Sparse" if frond else "Leaf Sparse",
                "generator_type": "Frond" if frond else "Leaf Mesh",
                "material_id": row["target_material_id"],
                "mesh_id": row["target_mesh_id"],
                "visible": True,
                "export_participates": True,
                "provider_scope": provider["scope"],
            })
    text = target.read_text(encoding="utf-8")
    return {
        "contract": "speedtree_live_generator_delivery_snapshot_v1",
        "spm": str(target),
        "spm_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "leaf_generator_bindings": bindings,
        "mesh_asset_ids": sorted({
            str(row["mesh_id"]) for row in bindings
        }),
    }


def _write_workspace(folder, *, phase="return_to_a_with_three_providers"):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / fixture["target_spm"]
    target.write_text("<SpeedTreeModel />", encoding="utf-8")
    scope_dir = folder / ".atlas_leaf_speedtree_scopes"
    target_dir = folder / ".atlas_leaf_speedtree_targets"
    scope_dir.mkdir()
    target_dir.mkdir()
    paths = {}
    for provider in fixture["providers"]:
        path = scope_dir / f"{provider['scope']}__{target.stem}.json"
        path.write_text(
            json.dumps(_payload(target, provider), indent=2),
            encoding="utf-8",
        )
        paths[provider["scope"]] = path
    latest = fixture["providers"][-1]
    exact = target_dir / f"{target.stem}.json"
    exact.write_text(
        json.dumps(_payload(target, latest), indent=2),
        encoding="utf-8",
    )
    paths["exact"] = exact
    return fixture, target, paths, _snapshot(target, fixture, phase)


def _scopes_by_current_bindings(plan):
    return {
        row["source_identity"]["declared_export_scope_id"]: {
            (
                binding["generator_guid"],
                binding["slot_prefix"],
                binding["target_material_id"],
                binding["target_mesh_id"],
            )
            for binding in row["current_bindings"]
        }
        for row in plan["provider_updates"]
    }


class AtlasSlotOwnershipTests(unittest.TestCase):
    def test_sparse_arbitrary_slots_and_repeated_takeover_follow_live_spm(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            fixture, target, _paths, snapshot = _write_workspace(
                Path(raw_folder)
            )

            plan = ownership.plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=snapshot,
            )

            self.assertEqual(plan["status"], "repairable")
            current = _scopes_by_current_bindings(plan)
            leaf = fixture["leaf_generator_guid"]
            frond = fixture["frond_generator_guid"]
            self.assertEqual(
                current["scope-a"],
                {
                    (leaf, "Leaves:Type:0", 10, 100),
                    (leaf, "Leaves:Type:7", 10, 107),
                },
            )
            self.assertEqual(current["scope-b"], set())
            self.assertEqual(
                current["scope-c"],
                {(leaf, "Leaves:Type:100", 30, 3100)},
            )
            self.assertEqual(
                current["scope-d"],
                {
                    (leaf, "Leaves:Type:42", 40, 442),
                    (frond, "Material:Frond:900", 40, 4900),
                },
            )
            self.assertGreaterEqual(len(plan["takeovers"]), 3)

            provider_a = next(
                row
                for row in plan["provider_updates"]
                if row["source_identity"]["declared_export_scope_id"]
                == "scope-a"
            )
            self.assertEqual(provider_a["creator_slot_count"], 3)

    def test_apply_is_atomic_and_second_plan_is_noop(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            fixture, target, paths, snapshot = _write_workspace(
                Path(raw_folder)
            )
            plan = ownership.plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=snapshot,
            )
            with mock.patch(
                "pcg_st9_texture_batch.pcg_texture_audit.live_generator_delivery_snapshot",
                return_value=snapshot,
            ):
                result = ownership.apply_atlas_slot_ownership_reconciliation(
                    plan
                )

            self.assertEqual(result["apply_status"], "reconciled")
            second = ownership.plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=snapshot,
            )
            self.assertEqual(second["status"], "current")
            self.assertEqual(second["writes"], [])

            provider_a = json.loads(
                paths["scope-a"].read_text(encoding="utf-8")
            )
            current_slots = {
                row["slot_prefix"]
                for row in provider_a["generator_binding_ownership"][
                    "bindings"
                ]
            }
            creator_slots = {
                row["slot_prefix"]
                for row in provider_a[
                    "generator_slot_creation_provenance"
                ]["slots"]
            }
            authored_slots = {
                row["slot_prefix"]
                for row in provider_a["generator_connection"][
                    "authored_bindings"
                ]
            }
            self.assertEqual(
                current_slots,
                {"Leaves:Type:0", "Leaves:Type:7"},
            )
            self.assertEqual(
                creator_slots,
                {
                    "Leaves:Type:7",
                    "Leaves:Type:42",
                    "Leaves:Type:100",
                },
            )
            self.assertEqual(
                authored_slots,
                {
                    row["slot_prefix"]
                    for row in fixture["providers"][0]["legacy_bindings"]
                },
            )

    def test_ambiguous_provider_pair_remains_blocked(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            fixture, target, paths, snapshot = _write_workspace(
                Path(raw_folder)
            )
            provider_b = json.loads(
                paths["scope-b"].read_text(encoding="utf-8")
            )
            provider_b["speedtree_material_groups"][0].update({
                "material_id": 10,
                "mesh_ids": [107],
            })
            provider_b["generator_connection"]["bindings"][0].update({
                "target_material_id": 10,
                "target_mesh_id": 107,
            })
            paths["scope-b"].write_text(
                json.dumps(provider_b, indent=2),
                encoding="utf-8",
            )

            plan = ownership.plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=snapshot,
            )

            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(
                plan["reason_code"],
                "managed_live_pair_provider_ambiguous",
            )

    def test_same_provider_live_rebind_is_current_and_old_pair_is_history(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            fixture, target, paths, snapshot = _write_workspace(
                Path(raw_folder)
            )
            leaf_guid = fixture["leaf_generator_guid"]
            rebound = next(
                row
                for row in snapshot["leaf_generator_bindings"]
                if row["generator_guid"] == leaf_guid
                and row["slot_prefix"] == "Leaves:Type:7"
            )
            rebound["mesh_id"] = 100

            plan = ownership.plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=snapshot,
            )

            self.assertEqual(plan["status"], "repairable")
            scope_a_write = next(
                row
                for row in plan["writes"]
                if Path(row["path"]) == paths["scope-a"].resolve()
            )
            current = {
                row["slot_prefix"]: row
                for row in scope_a_write["payload"][
                    "generator_binding_ownership"
                ]["bindings"]
            }
            self.assertEqual(current["Leaves:Type:7"]["target_mesh_id"], 100)
            history = scope_a_write["payload"]["generator_connection"][
                "relinquished_bindings"
            ]
            self.assertTrue(any(
                row.get("slot_prefix") == "Leaves:Type:7"
                and row.get("target_mesh_id") == 107
                and row.get("live_target_mesh_id") == 100
                and row.get("reason") == "live_spm_same_provider_rebound"
                for row in history
            ))

    def test_default_cutout_minus_ten_is_valid_without_prefix_assumptions(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            fixture, target, _paths, snapshot = _write_workspace(
                Path(raw_folder)
            )
            leaf_guid = fixture["leaf_generator_guid"]
            default_cutout = next(
                row
                for row in snapshot["leaf_generator_bindings"]
                if row["generator_guid"] == leaf_guid
                and row["slot_prefix"] == "Leaves:Type:7"
            )
            default_cutout["mesh_id"] = -10

            plan = ownership.plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=snapshot,
            )

            self.assertEqual(plan["status"], "repairable")
            current = _scopes_by_current_bindings(plan)
            self.assertIn(
                (leaf_guid, "Leaves:Type:7", 10, -10),
                current["scope-a"],
            )

    def test_manifest_cas_rejects_drift_without_writes(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            _fixture, target, _paths, snapshot = _write_workspace(
                Path(raw_folder)
            )
            plan = ownership.plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=snapshot,
            )
            changed = Path(plan["writes"][0]["path"])
            changed.write_bytes(changed.read_bytes() + b"\n")

            with self.assertRaises(ownership.AtlasSlotOwnershipError) as raised:
                ownership.apply_atlas_slot_ownership_reconciliation(plan)

            self.assertEqual(
                raised.exception.reason_code,
                "manifest_precondition_changed",
            )

    def test_mid_transaction_failure_restores_exact_original_bytes(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            _fixture, target, _paths, snapshot = _write_workspace(
                Path(raw_folder)
            )
            plan = ownership.plan_atlas_slot_ownership_reconciliation(
                target,
                live_snapshot=snapshot,
            )
            originals = {
                Path(row["path"]): Path(row["path"]).read_bytes()
                for row in plan["writes"]
            }
            real_write = ownership._atomic_write_bytes
            calls = {"count": 0}

            def fail_once(path, encoded):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected replacement failure")
                return real_write(path, encoded)

            with mock.patch.object(
                ownership,
                "_atomic_write_bytes",
                side_effect=fail_once,
            ):
                with self.assertRaises(ownership.AtlasSlotOwnershipError):
                    ownership.apply_atlas_slot_ownership_reconciliation(plan)

            self.assertEqual(
                {path: path.read_bytes() for path in originals},
                originals,
            )

    def test_fleet_apply_reconciles_multiple_targets_as_one_plan(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            root = Path(raw_folder)
            first = _write_workspace(root / "first")
            second = _write_workspace(
                root / "second",
                phase="b_to_c_and_add_high",
            )
            snapshots = {
                str(first[1]): first[3],
                str(second[1]): second[3],
            }
            fleet = ownership.plan_atlas_slot_ownership_fleet(
                [first[1], second[1]],
                live_snapshots=snapshots,
            )
            self.assertEqual(fleet["status"], "repairable")
            self.assertEqual(fleet["target_count"], 2)

            def current_snapshot(path):
                return snapshots[str(Path(path).resolve())]

            with mock.patch(
                "pcg_st9_texture_batch.pcg_texture_audit.live_generator_delivery_snapshot",
                side_effect=current_snapshot,
            ):
                result = ownership.apply_atlas_slot_ownership_fleet(fleet)

            self.assertEqual(result["apply_status"], "reconciled")
            current = ownership.plan_atlas_slot_ownership_fleet(
                [first[1], second[1]],
                live_snapshots=snapshots,
            )
            self.assertEqual(current["status"], "current")
            self.assertEqual(current["write_count"], 0)

    def test_fleet_failure_rolls_back_targets_already_written(self):
        with tempfile.TemporaryDirectory() as raw_folder:
            root = Path(raw_folder)
            first = _write_workspace(root / "first")
            second = _write_workspace(root / "second")
            snapshots = {
                str(first[1]): first[3],
                str(second[1]): second[3],
            }
            fleet = ownership.plan_atlas_slot_ownership_fleet(
                [first[1], second[1]],
                live_snapshots=snapshots,
            )
            rows = [
                row
                for child in fleet["target_plans"]
                for row in child["writes"]
            ]
            originals = {
                Path(row["path"]): Path(row["path"]).read_bytes()
                for row in rows
            }
            real_write = ownership._atomic_write_bytes
            calls = {"count": 0}
            fail_at = len(first[0]["providers"]) + 2

            def fail_once(path, encoded):
                calls["count"] += 1
                if calls["count"] == fail_at:
                    raise OSError("injected fleet replacement failure")
                return real_write(path, encoded)

            with mock.patch.object(
                ownership,
                "_atomic_write_bytes",
                side_effect=fail_once,
            ):
                with self.assertRaises(ownership.AtlasSlotOwnershipError):
                    ownership.apply_atlas_slot_ownership_fleet(fleet)

            self.assertEqual(
                {path: path.read_bytes() for path in originals},
                originals,
            )


if __name__ == "__main__":
    unittest.main()
