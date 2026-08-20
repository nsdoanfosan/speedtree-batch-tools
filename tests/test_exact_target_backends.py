import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import atlas_slot_ownership as ownership
from atlas_producer_rebind import build_atlas_producer_rebind_proof
from atlas_target_registry import (
    load_target_registry,
    registry_path_for_blend,
    save_target_registry,
)
from artifact_content_key import (
    LEGACY_FINGERPRINT_ALGORITHM,
    SAMPLED_FINGERPRINT_ALGORITHM,
    SHA256_ALGORITHM,
    file_content_key_snapshot,
)
from cluster_spm_pair_contract import bootstrap_cluster_authoring
from pcg_st9_texture_batch import exact_target_repair as pcg_exact
from spm_generator_sync import exact_target_repair as generator_exact


class ExactTargetBackendTests(unittest.TestCase):
    @staticmethod
    def artifact_record(path, algorithm):
        snapshot = file_content_key_snapshot(path, algorithm)
        record = {
            "path": str(Path(path).absolute()),
            "size": snapshot["size"],
            "mtime_ns": snapshot["mtime_ns"],
        }
        if algorithm == SHA256_ALGORITHM:
            record["sha256"] = snapshot["digest"]
        else:
            record["fingerprint"] = snapshot["digest"]
            record["fingerprint_algorithm"] = algorithm
        return record

    def sealed_off_cluster_relation(self, root):
        root = Path(root)
        owner = root / "Tree"
        cluster = owner / "cluster"
        fbx_dir = owner / "fbx"
        cluster.mkdir(parents=True)
        fbx_dir.mkdir()
        target = owner / "SK_tree.spm"
        provider = cluster / "SK_cluster.spm"
        blend = provider.with_suffix(".blend")
        fbx = fbx_dir / "SK_tree.fbx"
        target.write_bytes(b"target")
        provider.write_bytes(b"provider")
        blend.write_bytes(b"blend")
        fbx.write_bytes(b"full-fbx")
        save_target_registry(blend, [])
        registry = registry_path_for_blend(blend)
        relation = {
            "schema_version": 1,
            "target_spm": str(target),
            "provider_spm": str(provider),
            "provider_blend": str(blend),
            "relation_status": "explicit_off",
            "relation_allowed": False,
            "live_pair_proof": {
                "current_live_pair_covered": True,
                "spm_pair_status": "complete_pair",
                "fbx_pair_status": "complete_pair",
                "fbx_pair_decision": "normalize_part",
            },
            "artifacts": {
                "target_spm": self.artifact_record(
                    target, LEGACY_FINGERPRINT_ALGORITHM
                ),
                "provider_spm": self.artifact_record(
                    provider, LEGACY_FINGERPRINT_ALGORITHM
                ),
                "provider_blend": self.artifact_record(
                    blend, SAMPLED_FINGERPRINT_ALGORITHM
                ),
                "target_fbx": self.artifact_record(
                    fbx, SAMPLED_FINGERPRINT_ALGORITHM
                ),
                "target_registry": self.artifact_record(
                    registry, SHA256_ALGORITHM
                ),
            },
        }
        return target, provider, blend, fbx, relation

    def ownership_plan(self, root):
        target = Path(root) / "SK_exact.spm"
        target.write_bytes(b"spm")
        manifest = Path(root) / "ownership.json"
        manifest.write_text("{}", encoding="utf-8")
        payload = {"spm": str(target), "generator_connection": {}}
        encoded = ownership._pretty_json_bytes(payload)
        plan = {
            "contract": ownership.PLAN_CONTRACT,
            "schema_version": ownership.PLAN_SCHEMA_VERSION,
            "target_spm": str(target),
            "status": "repairable",
            "reason_code": "live_spm_ownership_reconciliation_required",
            "spm_sha256": "a" * 64,
            "spm_text_sha256": "b" * 64,
            "manifest_preconditions": [{
                "path": str(manifest),
                "sha256": "c" * 64,
            }],
            "provider_updates": [],
            "takeovers": [],
            "blocking": [],
            "ignored_candidates": [],
            "writes": [{
                "path": str(manifest),
                "before_sha256": "c" * 64,
                "after_sha256": ownership._sha256_bytes(encoded),
                "payload": payload,
            }],
        }
        plan["plan_sha256"] = ownership._plan_hash(plan)
        return target, plan

    def ownership_request(self, target, plan):
        return {
            "repair_action": generator_exact.ATLAS_SLOT_OWNERSHIP_RECONCILE,
            "target_spms": [str(target)],
            "provenance": {"ownership_plan": copy.deepcopy(plan)},
        }

    def producer_relation(self, root):
        root = Path(root)
        cluster = root / "Tree" / "cluster"
        cluster.mkdir(parents=True)
        legacy = cluster / "cluster_leaf.spm"
        legacy.write_bytes(b"legacy")
        bootstrap_cluster_authoring(legacy)
        canonical = cluster / "SK_cluster_leaf.spm"
        blend = root / "atlas" / "M_leaf.blend"
        blend.parent.mkdir()
        blend.write_bytes(b"blend")
        unrelated = root / "Tree" / "SK_tree.spm"
        unrelated.write_bytes(b"tree")
        save_target_registry(blend, [unrelated, legacy])
        manifest = (
            cluster
            / ".atlas_leaf_speedtree_targets"
            / f"{legacy.stem}.json"
        )
        manifest.parent.mkdir()
        albedo = cluster / "leaf.tga"
        alpha = cluster / "leaf_Opacity.tga"
        albedo.write_bytes(b"albedo")
        alpha.write_bytes(b"alpha")
        manifest.write_text(json.dumps({
            "spm": str(legacy),
            "blend_file": str(blend),
            "source_collection": "M_leaf",
            "export_scope_id": "scope-leaf",
            "atlas_asset_name": "M_leaf",
            "material_groups": [{
                "material_id": 5,
                "mesh_ids": [3, 4, 5, 6],
            }],
            "textures": {"albedo": str(albedo), "alpha": str(alpha)},
            "generator_connection": {
                "requested": False,
                "complete": False,
            },
        }), encoding="utf-8")
        proof = build_atlas_producer_rebind_proof(
            canonical,
            manifest,
            inventory_paths=[canonical],
        )
        request = {
            "repair_action": pcg_exact.ATLAS_PRODUCER_REFRESH,
            "target_spms": [str(canonical)],
            "provenance": {"producer_relation": copy.deepcopy(proof)},
            "request_id": "producer-test",
        }
        return request, proof, blend, legacy, canonical

    def test_pcg_inventory_selection_requires_one_exact_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "SK_exact.spm"
            sibling = root / "SK_sibling.spm"
            target.write_bytes(b"target")
            sibling.write_bytes(b"sibling")

            class Module:
                @staticmethod
                def spm_paths_for_item(item):
                    return item["paths"]

            selected, inventory = pcg_exact._exact_item(
                Module,
                {"items": [
                    {"folder": str(root), "paths": [str(target)]},
                    {"folder": str(root / "other"), "paths": [str(sibling)]},
                ]},
                target,
            )
            self.assertEqual(selected["paths"], [str(target)])
            self.assertEqual(set(inventory), {str(target), str(sibling)})

    def test_pcg_consumer_rows_do_not_fan_out_to_sibling_spms(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "SK_exact.spm"
            sibling = root / "SK_sibling.spm"
            rows = [{
                "atlas_base": "T_Exact",
                "material_spms": [str(target)],
                "material_targets": [{"spm": str(target), "material_id": "4"}],
            }, {
                "atlas_base": "T_Sibling",
                "material_spms": [str(sibling)],
                "material_targets": [{"spm": str(sibling), "material_id": "7"}],
            }]
            selected = pcg_exact._rows_for_exact_target(rows, target)
            self.assertEqual([row["atlas_base"] for row in selected], ["T_Exact"])
            self.assertEqual(
                selected[0]["material_targets"],
                [{"spm": str(target), "material_id": "4"}],
            )

    def test_generator_scope_filters_sibling_followers_and_cluster_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            asset = root / "asset"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            master = asset / "SK_master.spm"
            follower = asset / "SK_exact.spm"
            sibling = asset / "SK_sibling.spm"
            cluster_target = cluster / "SK_cluster.spm"
            other_cluster = cluster / "SK_other_cluster.spm"
            for path in (master, follower, sibling, cluster_target, other_cluster):
                path.write_bytes(path.name.encode())

            scope = {
                "groups": [{
                    "folder": asset,
                    "master": master.name,
                    "names": [follower.name, sibling.name],
                }, {
                    "folder": cluster,
                    "master": cluster_target.name,
                    "names": [other_cluster.name],
                }],
                "cluster_rows": [{
                    "blend": cluster / "SK_cluster.blend",
                    "on_target_spms": [cluster_target, other_cluster],
                }],
                "skipped": [],
            }

            class Engine:
                @staticmethod
                def scan_tree_folders(*_args, **_kwargs):
                    return ["board"]

            class App:
                @staticmethod
                def _connected_scope_from_board(_board):
                    return scope

            module = SimpleNamespace(
                engine=Engine(),
                App=App,
                load_config=lambda: {"tree_root": str(root), "sk_only": True},
            )

            with mock.patch.object(
                generator_exact, "_load_gui_module", return_value=module
            ):
                _module, _cfg, _root, groups, rows, canonical = (
                    generator_exact.exact_runtime_scope(
                        [follower, cluster_target]
                    )
                )

            self.assertEqual(groups[0]["names"], [follower.name])
            self.assertEqual(rows[0]["on_target_spms"], [cluster_target])
            self.assertNotIn(sibling.name, groups[0]["names"])
            self.assertNotIn(other_cluster, rows[0]["on_target_spms"])
            self.assertEqual(canonical, [str(follower), str(cluster_target)])

    def test_exact_provider_scope_skips_full_board_and_other_providers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            owner = root / "asset"
            cluster = owner / "cluster"
            cluster.mkdir(parents=True)
            target = owner / "SK_tree_exact.spm"
            target.write_bytes(b"target")
            selected = cluster / "SK_branch_exact.blend"
            selected.write_bytes(b"blend")
            selected.with_suffix(".spm").write_bytes(b"provider")
            other = cluster / "SK_leaf_other.blend"
            other.write_bytes(b"other")
            other.with_suffix(".spm").write_bytes(b"other-provider")
            save_target_registry(selected, [target])
            save_target_registry(other, [target])

            class Engine:
                @staticmethod
                def scan_tree_folders(*_args, **_kwargs):
                    raise AssertionError("full board scan must be skipped")

            module = SimpleNamespace(
                engine=Engine(),
                App=SimpleNamespace(),
                load_config=lambda: {
                    "tree_root": str(root),
                    "sk_only": True,
                },
            )
            with mock.patch.object(
                generator_exact, "_load_gui_module", return_value=module
            ):
                _module, _cfg, _root, groups, rows, canonical = (
                    generator_exact.exact_runtime_scope(
                        [target],
                        provider_blends=[selected],
                    )
                )

            self.assertEqual(groups, [])
            self.assertEqual(canonical, [str(target)])
            self.assertEqual(len(rows), 1)
            self.assertEqual(Path(rows[0]["blend"]), selected)
            self.assertEqual(rows[0]["on_target_spms"], [target])

    def test_generator_master_only_request_fails_instead_of_fanning_out(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            master = root / "SK_master.spm"
            follower = root / "SK_follower.spm"
            master.write_bytes(b"master")
            follower.write_bytes(b"follower")

            class Engine:
                @staticmethod
                def scan_tree_folders(*_args, **_kwargs):
                    return []

            class App:
                @staticmethod
                def _connected_scope_from_board(_board):
                    return {
                        "groups": [{
                            "folder": root,
                            "master": master.name,
                            "names": [follower.name],
                        }],
                        "cluster_rows": [],
                    }

            module = SimpleNamespace(
                engine=Engine(),
                App=App,
                load_config=lambda: {"tree_root": str(root), "sk_only": True},
            )

            with mock.patch.object(
                generator_exact, "_load_gui_module", return_value=module
            ):
                with self.assertRaisesRegex(ValueError, "sibling followers"):
                    generator_exact.exact_runtime_scope([master])

    def test_generator_cluster_plan_accepts_live_row_source_spm(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            asset = root / "asset"
            cluster = asset / "cluster"
            cluster.mkdir(parents=True)
            master = asset / "SK_tree_master.spm"
            target = asset / "SK_tree_exact.spm"
            provider = cluster / "SK_cluster_exact.spm"
            for path in (master, target, provider):
                path.write_bytes(path.name.encode())

            scope = {
                "groups": [{
                    "folder": asset,
                    "master": master.name,
                    "names": [target.name],
                }],
                "cluster_rows": [{
                    "blend": provider.with_suffix(".blend"),
                    "source_spm": provider,
                    "canonical_spm": provider,
                    "on_target_spms": [target],
                }],
                "skipped": [],
            }

            class Engine:
                @staticmethod
                def scan_tree_folders(*_args, **_kwargs):
                    return ["board"]

            class App:
                @staticmethod
                def _connected_scope_from_board(_board):
                    return scope

            module = SimpleNamespace(
                engine=Engine(),
                App=App,
                load_config=lambda: {
                    "tree_root": str(root),
                    "sk_only": True,
                },
            )

            with mock.patch.object(
                generator_exact, "_load_gui_module", return_value=module
            ):
                _module, _cfg, _root, groups, rows, canonical = (
                    generator_exact.exact_runtime_scope([target, provider])
                )

            self.assertEqual(groups[0]["names"], [target.name])
            self.assertEqual(rows[0]["on_target_spms"], [target])
            self.assertEqual(canonical, [str(target), str(provider)])

    def test_sealed_explicit_off_relation_becomes_one_exact_cluster_row(self):
        with tempfile.TemporaryDirectory() as folder:
            target, provider, blend, _fbx, relation = (
                self.sealed_off_cluster_relation(folder)
            )

            class Engine:
                @staticmethod
                def scan_tree_folders(*_args, **_kwargs):
                    return []

            class App:
                @staticmethod
                def _connected_scope_from_board(_board):
                    return {"groups": [], "cluster_rows": []}

            module = SimpleNamespace(
                engine=Engine(),
                App=App,
                load_config=lambda: {
                    "tree_root": str(Path(folder)),
                    "sk_only": True,
                },
            )
            with mock.patch.object(
                generator_exact, "_load_gui_module", return_value=module
            ):
                _module, _cfg, _root, groups, rows, canonical = (
                    generator_exact.exact_runtime_scope(
                        [target],
                        cluster_provider_relations=[relation],
                    )
                )

            self.assertEqual(groups, [])
            self.assertEqual(canonical, [str(target)])
            self.assertEqual(len(rows), 1)
            self.assertEqual(Path(rows[0]["blend"]), blend)
            self.assertEqual(rows[0]["on_target_spms"], [target])
            self.assertEqual(Path(rows[0]["source_spm"]), provider)

    def test_sealed_relation_rejects_changed_live_fbx_before_registry_write(self):
        with tempfile.TemporaryDirectory() as folder:
            target, _provider, _blend, fbx, relation = (
                self.sealed_off_cluster_relation(folder)
            )
            fbx.write_bytes(b"changed-full-fbx")

            class Engine:
                @staticmethod
                def scan_tree_folders(*_args, **_kwargs):
                    return []

            class App:
                @staticmethod
                def _connected_scope_from_board(_board):
                    return {"groups": [], "cluster_rows": []}

            module = SimpleNamespace(
                engine=Engine(),
                App=App,
                load_config=lambda: {
                    "tree_root": str(Path(folder)),
                    "sk_only": True,
                },
            )
            with mock.patch.object(
                generator_exact, "_load_gui_module", return_value=module
            ), self.assertRaisesRegex(ValueError, "artifact changed"):
                generator_exact.exact_runtime_scope(
                    [target],
                    cluster_provider_relations=[relation],
                )

    def test_exact_multi_unit_run_rebases_authorized_successor_identity(self):
        first = {
            "unit_id": "cluster:first",
            "stage": "cluster_refresh",
            "resource_sets": {"write": [{
                "kind": "file",
                "path": "shared.spm",
            }]},
        }
        second = {
            "unit_id": "cluster:second",
            "stage": "cluster_refresh",
            "resource_sets": {"read": [{
                "kind": "file",
                "path": "shared.spm",
            }]},
        }
        observed = []

        class App:
            def _execute_connected_runtime_unit(
                self,
                unit,
                _runtime,
                _cfg,
                _verify,
                _settings,
                expected_identity,
                *_reports,
            ):
                observed.append((unit["unit_id"], copy.deepcopy(
                    expected_identity
                )))
                return {
                    "ok": True,
                    "result": {"status": "ok"},
                }

            @staticmethod
            def _connected_result_summary(result):
                return copy.deepcopy(result)

        module = SimpleNamespace(App=App)
        identities = {
            first["unit_id"]: {"digest": "first-before"},
            second["unit_id"]: {"digest": "second-before"},
        }
        rebased = {
            second["unit_id"]: {
                "authorized_by_unit_id": first["unit_id"],
                "previous_digest": "second-before",
                "digest": "second-after",
                "changed_resources": [{
                    "kind": "file",
                    "path": "shared.spm",
                }],
                "identity": {"digest": "second-after"},
            },
        }
        request = {
            "repair_action": generator_exact.CLUSTER_REFRESH,
            "target_spms": ["target.spm"],
        }

        with mock.patch.object(
            generator_exact,
            "build_exact_runtime_plan",
            return_value=(
                module,
                {"verify_speedtree": True},
                [first, second],
                [{"row": 1}, {"row": 2}],
                identities,
                {"settings": True},
                ["target.spm"],
            ),
        ), mock.patch.object(
            generator_exact,
            "rebase_authorized_dependency_identities",
            side_effect=[rebased, {}],
        ) as rebase:
            result = generator_exact.execute_exact_generator_request(
                request,
                progress=mock.Mock(),
                cancel_event=SimpleNamespace(is_set=lambda: False),
                lease=SimpleNamespace(
                    renew_and_check_current=lambda: True
                ),
            )

        self.assertTrue(result["shared_queue_success"])
        self.assertEqual(
            observed,
            [
                (first["unit_id"], {"digest": "first-before"}),
                (second["unit_id"], {"digest": "second-after"}),
            ],
        )
        self.assertEqual(rebase.call_count, 2)
        self.assertEqual(
            result["units"][0]["authorized_dependency_rebases"][0][
                "digest"
            ],
            "second-after",
        )
        self.assertNotIn(
            "identity",
            result["units"][0]["authorized_dependency_rebases"][0],
        )

    def test_exact_current_cluster_scope_skips_heavy_identity_and_worker(self):
        units = [
            {"unit_id": "cluster:first", "stage": "cluster_refresh"},
            {"unit_id": "cluster:second", "stage": "cluster_refresh"},
        ]
        rows = [
            {
                "blend": "C:/Tree/Cluster/SK_branch_01.blend",
                "on_target_spms": ["C:/Tree/SK_tree_01.spm"],
            },
            {
                "blend": "C:/Tree/Cluster/SK_leaf_01.blend",
                "on_target_spms": ["C:/Tree/SK_tree_01.spm"],
            },
        ]

        class App:
            def _execute_connected_runtime_unit(self, *_args, **_kwargs):
                raise AssertionError("current relations must not reach worker")

            @staticmethod
            def _connected_result_summary(result):
                return copy.deepcopy(result)

        module = SimpleNamespace(App=App)
        current_state = {
            "current": True,
            "registered": True,
            "verification": {"status": "ok"},
            "targets": [{
                "source_content_identity": {
                    "status": "current",
                    "current": True,
                },
            }],
            "refresh_reasons": [],
            "refresh_reason_categories": [],
        }
        request = {
            "repair_action": generator_exact.CLUSTER_REFRESH,
            "target_spms": ["C:/Tree/SK_tree_01.spm"],
        }

        with mock.patch.object(
            generator_exact,
            "build_exact_runtime_plan",
            return_value=(
                module,
                {"verify_speedtree": True},
                units,
                rows,
                {},
                {"board_root": "C:/Tree"},
                ["C:/Tree/SK_tree_01.spm"],
            ),
        ) as build, mock.patch.object(
            generator_exact,
            "inspect_cluster_relation_current_state",
            return_value=current_state,
        ) as inspect, mock.patch.object(
            generator_exact,
            "scope_dependency_identities",
        ) as identities:
            result = generator_exact.execute_exact_generator_request(
                request,
                progress=mock.Mock(),
                cancel_event=SimpleNamespace(is_set=lambda: False),
                lease=SimpleNamespace(
                    renew_and_check_current=lambda: True
                ),
            )

        build.assert_called_once_with(request, include_identities=False)
        self.assertEqual(inspect.call_count, 2)
        identities.assert_not_called()
        self.assertTrue(result["shared_queue_success"])
        self.assertTrue(result["fast_current_check"])
        self.assertEqual(len(result["units"]), 2)
        self.assertTrue(result["units"][0]["result"]["no_change"])

    def test_ownership_backend_plan_only_callable_never_applies(self):
        with tempfile.TemporaryDirectory() as folder:
            target, plan = self.ownership_plan(folder)
            request = self.ownership_request(target, plan)

            with mock.patch.object(
                generator_exact,
                "plan_atlas_slot_ownership_reconciliation",
                return_value=copy.deepcopy(plan),
            ), mock.patch.object(
                generator_exact,
                "apply_atlas_slot_ownership_reconciliation",
            ) as apply:
                fresh = generator_exact.build_exact_atlas_slot_ownership_plan(
                    request
                )

            self.assertEqual(fresh["plan_sha256"], plan["plan_sha256"])
            apply.assert_not_called()

    def test_ownership_backend_applies_only_fresh_matching_sealed_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            target, plan = self.ownership_plan(folder)
            request = self.ownership_request(target, plan)
            progress = mock.Mock()
            cancel = SimpleNamespace(is_set=lambda: False)
            lease = SimpleNamespace(renew_and_check_current=lambda: True)

            with mock.patch.object(
                generator_exact,
                "plan_atlas_slot_ownership_reconciliation",
                return_value=copy.deepcopy(plan),
            ), mock.patch.object(
                generator_exact,
                "apply_atlas_slot_ownership_reconciliation",
                return_value={"apply_status": "reconciled"},
            ) as apply:
                result = (
                    generator_exact.execute_exact_atlas_slot_ownership_request(
                        request,
                        progress=progress,
                        cancel_event=cancel,
                        lease=lease,
                    )
                )

            self.assertTrue(result["shared_queue_success"])
            apply.assert_called_once()
            self.assertEqual(
                apply.call_args.args[0]["plan_sha256"],
                plan["plan_sha256"],
            )

    def test_ownership_backend_rejects_changed_fresh_plan_before_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            target, plan = self.ownership_plan(folder)
            request = self.ownership_request(target, plan)
            changed = copy.deepcopy(plan)
            changed["takeovers"] = [{"reason_code": "changed"}]
            changed["plan_sha256"] = ownership._plan_hash(changed)

            with mock.patch.object(
                generator_exact,
                "plan_atlas_slot_ownership_reconciliation",
                return_value=changed,
            ), mock.patch.object(
                generator_exact,
                "apply_atlas_slot_ownership_reconciliation",
            ) as apply, self.assertRaises(
                ownership.AtlasSlotOwnershipError
            ) as raised:
                generator_exact.build_exact_atlas_slot_ownership_plan(request)

            self.assertEqual(
                raised.exception.reason_code,
                "exact_live_spm_ownership_plan_changed",
            )
            apply.assert_not_called()

    def test_producer_refresh_plan_is_read_only_and_exact(self):
        with tempfile.TemporaryDirectory() as folder:
            request, proof, blend, legacy, canonical = (
                self.producer_relation(folder)
            )

            plan = pcg_exact.build_exact_atlas_producer_refresh_plan(request)

            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["canonical_spm"], str(canonical))
            self.assertEqual(
                plan["proof"]["proof_sha256"], proof["proof_sha256"]
            )
            self.assertIn(
                str(legacy), load_target_registry(blend)["target_spms"]
            )

    def test_producer_refresh_real_authority_seals_every_exact_write_path(self):
        with tempfile.TemporaryDirectory() as folder:
            request, _proof, _blend, _legacy, canonical = (
                self.producer_relation(folder)
            )
            plan = pcg_exact.build_exact_atlas_producer_refresh_plan(request)
            pcg_exact.apply_atlas_producer_registry_rebind(
                plan["registry_plan"]
            )
            module = pcg_exact._load_gui_module()
            reports = Path(folder) / "reports"
            captured = {}

            def stop_after_capture(command, **_kwargs):
                authority_path = Path(
                    command[command.index("--authority-json") + 1]
                )
                captured.update(json.loads(
                    authority_path.read_text(encoding="utf-8")
                ))
                return SimpleNamespace(
                    returncode=1,
                    stderr="capture sentinel",
                    stdout="",
                )

            with mock.patch.object(
                module,
                "REPORT_DIR",
                reports,
            ), mock.patch.object(
                module,
                "load_config",
                return_value={
                    "blender_exe": str(Path(__file__).resolve()),
                    "atlas_job_timeout": 5,
                },
            ), mock.patch.object(
                module,
                "owned_run",
                side_effect=stop_after_capture,
            ), self.assertRaisesRegex(RuntimeError, "capture sentinel"):
                pcg_exact._run_exact_atlas_producer_refresh(request, plan)

            sealed = {
                str(Path(path).absolute()).casefold()
                for path in captured["path_names"]
            }
            writes = {
                str(Path(path).absolute()).casefold()
                for path in captured["write_path_names"]
            }
            expected = {
                str(path.absolute()).casefold()
                for path in (
                    canonical,
                    canonical.parent
                    / ".atlas_leaf_speedtree_targets"
                    / f"{canonical.stem}.json",
                    canonical.parent / "speedtree_import_manifest.json",
                    canonical.parent / "README_SPEEDTREE_IMPORT.md",
                )
            }
            self.assertTrue(writes.issubset(sealed))
            self.assertTrue(expected.issubset(sealed))

    def test_producer_refresh_retry_accepts_already_rebound_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            request, _proof, blend, legacy, canonical = (
                self.producer_relation(folder)
            )
            first = pcg_exact.build_exact_atlas_producer_refresh_plan(
                request
            )
            applied = pcg_exact.apply_atlas_producer_registry_rebind(
                first["registry_plan"]
            )

            retry = pcg_exact.build_exact_atlas_producer_refresh_plan(
                request
            )
            second = pcg_exact.apply_atlas_producer_registry_rebind(
                retry["registry_plan"]
            )

            self.assertEqual(applied["status"], "applied")
            self.assertEqual(retry["registry_status"], "already_rebound")
            self.assertEqual(second["status"], "up_to_date")
            targets = load_target_registry(blend)["target_spms"]
            self.assertIn(str(canonical), targets)
            self.assertNotIn(str(legacy), targets)

    def test_producer_refresh_executor_uses_rebind_then_direct_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            request, _proof, _blend, _legacy, _canonical = (
                self.producer_relation(folder)
            )
            progress = mock.Mock()
            cancel = SimpleNamespace(is_set=lambda: False)
            lease = SimpleNamespace(renew_and_check_current=lambda: True)

            with mock.patch.object(
                pcg_exact,
                "apply_atlas_producer_registry_rebind",
                return_value={"status": "applied"},
            ) as rebind, mock.patch.object(
                pcg_exact,
                "_run_exact_atlas_producer_refresh",
                return_value={"canonical_receipt": {"status": "validated"}},
            ) as producer:
                result = pcg_exact.execute_exact_atlas_producer_refresh(
                    request,
                    progress=progress,
                    cancel_event=cancel,
                    lease=lease,
                )

            self.assertTrue(result["shared_queue_success"])
            rebind.assert_called_once()
            producer.assert_called_once()

    def test_producer_failure_rolls_registry_back_to_exact_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            request, _proof, blend, legacy, _canonical = (
                self.producer_relation(folder)
            )
            registry_path = registry_path_for_blend(blend)
            original_bytes = registry_path.read_bytes()
            cancel = SimpleNamespace(is_set=lambda: False)
            lease = SimpleNamespace(renew_and_check_current=lambda: True)

            with mock.patch.object(
                pcg_exact,
                "_run_exact_atlas_producer_refresh",
                side_effect=RuntimeError("Blender producer failed"),
            ), self.assertRaisesRegex(
                RuntimeError,
                "Blender producer failed",
            ):
                pcg_exact.execute_exact_atlas_producer_refresh(
                    request,
                    progress=mock.Mock(),
                    cancel_event=cancel,
                    lease=lease,
                )

            self.assertEqual(registry_path.read_bytes(), original_bytes)
            self.assertIn(
                str(legacy), load_target_registry(blend)["target_spms"]
            )

    def test_cancel_after_rebind_rolls_registry_back_before_producer(self):
        with tempfile.TemporaryDirectory() as folder:
            request, _proof, blend, legacy, _canonical = (
                self.producer_relation(folder)
            )
            registry_path = registry_path_for_blend(blend)
            original_bytes = registry_path.read_bytes()
            cancel_check = mock.Mock(side_effect=[False, True])
            cancel = SimpleNamespace(is_set=cancel_check)
            lease = SimpleNamespace(renew_and_check_current=lambda: True)

            with mock.patch.object(
                pcg_exact,
                "_run_exact_atlas_producer_refresh",
            ) as producer, self.assertRaises(pcg_exact.WaitCancelled):
                pcg_exact.execute_exact_atlas_producer_refresh(
                    request,
                    progress=mock.Mock(),
                    cancel_event=cancel,
                    lease=lease,
                )

            producer.assert_not_called()
            self.assertEqual(registry_path.read_bytes(), original_bytes)
            self.assertIn(
                str(legacy), load_target_registry(blend)["target_spms"]
            )

    def test_lease_loss_after_rebind_rolls_registry_back(self):
        with tempfile.TemporaryDirectory() as folder:
            request, _proof, blend, legacy, _canonical = (
                self.producer_relation(folder)
            )
            registry_path = registry_path_for_blend(blend)
            original_bytes = registry_path.read_bytes()
            renew = mock.Mock(side_effect=[True, False])
            lease = SimpleNamespace(renew_and_check_current=renew)

            with mock.patch.object(
                pcg_exact,
                "_run_exact_atlas_producer_refresh",
            ) as producer, self.assertRaisesRegex(
                RuntimeError,
                "lease became stale",
            ):
                pcg_exact.execute_exact_atlas_producer_refresh(
                    request,
                    progress=mock.Mock(),
                    cancel_event=SimpleNamespace(is_set=lambda: False),
                    lease=lease,
                )

            producer.assert_not_called()
            self.assertEqual(registry_path.read_bytes(), original_bytes)
            self.assertIn(
                str(legacy), load_target_registry(blend)["target_spms"]
            )

    def test_registry_rollback_drift_preserves_both_errors_and_external_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            request, _proof, blend, _legacy, canonical = (
                self.producer_relation(folder)
            )
            external = Path(folder) / "Tree" / "SK_external.spm"
            external.write_bytes(b"external")

            def drift_then_fail(_request, _plan):
                save_target_registry(blend, [canonical, external])
                raise RuntimeError("Blender producer failed")

            with mock.patch.object(
                pcg_exact,
                "_run_exact_atlas_producer_refresh",
                side_effect=drift_then_fail,
            ), self.assertRaises(
                pcg_exact.AtlasProducerRefreshRollbackError
            ) as raised:
                pcg_exact.execute_exact_atlas_producer_refresh(
                    request,
                    progress=mock.Mock(),
                    cancel_event=SimpleNamespace(is_set=lambda: False),
                    lease=SimpleNamespace(
                        renew_and_check_current=lambda: True
                    ),
                )

            self.assertEqual(
                str(raised.exception.original_error),
                "Blender producer failed",
            )
            self.assertIn(
                "changed after authority seal",
                str(raised.exception.rollback_error),
            )
            self.assertEqual(
                load_target_registry(blend)["target_spms"],
                [str(canonical.absolute()), str(external.absolute())],
            )

    def test_committed_canonical_receipt_keeps_registry_on_child_error(self):
        with tempfile.TemporaryDirectory() as folder:
            request, proof, blend, legacy, canonical = (
                self.producer_relation(folder)
            )

            def commit_then_report_failure(_request, _plan):
                legacy_manifest = Path(proof["legacy_manifest"]["path"])
                payload = json.loads(
                    legacy_manifest.read_text(encoding="utf-8")
                )
                canonical_manifest = (
                    canonical.parent
                    / ".atlas_leaf_speedtree_targets"
                    / f"{canonical.stem}.json"
                )
                payload["spm"] = str(canonical)
                payload["target_manifest"] = str(canonical_manifest)
                canonical_manifest.write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
                raise RuntimeError("child report was lost")

            with mock.patch.object(
                pcg_exact,
                "_run_exact_atlas_producer_refresh",
                side_effect=commit_then_report_failure,
            ), self.assertRaises(
                pcg_exact.AtlasProducerRefreshCommittedError
            ) as raised:
                pcg_exact.execute_exact_atlas_producer_refresh(
                    request,
                    progress=mock.Mock(),
                    cancel_event=SimpleNamespace(is_set=lambda: False),
                    lease=SimpleNamespace(
                        renew_and_check_current=lambda: True
                    ),
                )

            self.assertEqual(
                str(raised.exception.original_error),
                "child report was lost",
            )
            targets = load_target_registry(blend)["target_spms"]
            self.assertIn(str(canonical.absolute()), targets)
            self.assertNotIn(str(legacy.absolute()), targets)


if __name__ == "__main__":
    unittest.main()
