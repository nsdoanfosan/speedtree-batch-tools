"""Save ownership and acknowledgement of an already completed native batch."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sk_batch import normalized_assembly_bend as bend
from sk_batch import prepared_rpc_resume as resume
from sk_batch.tests import test_prepared_rpc_resume as fixtures


class CompletedRpcTests(unittest.TestCase):
    setUp = fixtures.PreparedRpcResumeTests.setUp
    file = fixtures.PreparedRpcResumeTests.file
    prepared = fixtures.PreparedRpcResumeTests.prepared

    def write(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return resume._file_record(path)

    def record(self, package, *, owner="normalized_part_bend_dependency", role="prototype", content=b"final-native"):
        path = self.content / (package[6:] + ".uasset")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"package": package, "asset": package, "package_file": str(path), "owner": owner,
                "role": role, "saved": True, "dirty_after_save": False, "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns}

    def normalized(self, item):
        self.content = self.root.parent / "Content"
        paths = ["/Game/Tree/ReferenceA", "/Game/Tree/ReferenceB", "/Game/Tree/CustomA", "/Game/Tree/CustomB", "/Game/Tree/CustomC"]
        roles = ["branch", "branch", "leaf", "leaf_side", "cluster"]
        mapping = dict(zip((f"p{i}" for i in range(5)), paths))
        manifest = item["cluster_assembly"]["manifest"]
        manifest["parts"] = [{"prototype_id": key, "role": role, "external_source": {
            "kind": "send_to_unreal_normalized_skeletal_part", "pivot_contract": "normalized_attachment_origin_0_0_0"}}
            for (key, path), role in zip(mapping.items(), roles)]
        contract = item["cluster_assembly"]["ingest_plan"]["asset_contract"]
        contract["parts"] = mapping
        parts = bend.plan_manifest_parts(manifest, contract, [{"mesh": path} for path in paths])
        for part in parts:
            if part["role"]:
                part.update(source_file="C:/Native/part.fbx", source_sha1="a" * 40)
        sources = [f"/Game/Material/Source{i}" for i in range(5)]
        variants = [bend.variant_path(path) if i in (1, 4) else path for i, path in enumerate(sources)]
        materials = [bend.variant_path("/Game/Material/M_RootA", master=True),
                     bend.variant_path("/Game/Material/M_RootB", master=True), variants[1], variants[4]]
        native = {"schema_version": 2, "operation": "configure_normalized_assembly_bend_native", "ok": True,
            "payload_checked_prototype_count": 3, "payload_verified_before_material_mutation": True,
            "production_prototype_materials_directly_applied": True, "root_source_mesh_description_preserved": True,
            "branch_materials_preserved": True, "part_count": 5, "active_source_indices": [1, 4], "material_count": 7,
            "prototype_material_checks": [{"asset": part["mesh"], "changed": True} for part in parts if part["role"]],
            "uv3_verifications": [{"prototype": part["mesh"], "role": part["role"], "import_file_sha1": part["source_sha1"],
                "native_import_md5_matches": True, "verified": True, "uv_channel_count": 4, "aspect_ratio": 4.0,
                "correction_factor": 2.0, "analytical_pivot_mask": 0.0} for part in parts if part["role"]]}
        normalization = {"assembly": contract["assembly"], "apply_requested": True, "native_material_remap": native,
            "parts": [{"index": part["index"], "mesh": part["mesh"], "existing_remap": [0, 1] if not part["role"] else [1, 4],
                       "desired_remap": [0, 1] if not part["role"] else [5, 6]} for part in parts],
            "dependency_save_status": "persisted_verified_clean", "normalized_part_bend_policy": {
                "aspect_policy": bend.ASPECT_POLICY, "material_packages": materials,
                "contract": {"schema_version": 2, "assembly": contract["assembly"], "parts": parts,
                             "source_materials": sources, "variant_materials": variants, "active_source_indices": [1, 4]}},
            "material_compile_proofs": [{"asset": path, "native_finish_compilation_waited": True,
                                         "shader_count": 1, "recompile_errors": []} for path in materials]}
        packages = [contract[name] for name in ("full_skeletal_mesh", "base_skeletal_mesh", "assembly")]
        records = [self.record(path, owner="final", role="mesh") for path in packages]
        records += [self.record(path, role="material") for path in materials]
        records += [self.record(part["mesh"]) for part in parts if part["role"]]
        for index, record in enumerate(records):
            record["sequence"] = index
        runtime = {"status": "passed", "begin": {"success": True, "status": "pending", "probe_token": "native-token"},
                   "finish": {"success": True, "status": "passed", "probe_token": "native-token"}}
        result = {"status": "imported_ok", "durable_saves": {"schema_version": 1, "records": records},
                  "cluster_assembly": {"status": "ok", "runtime": runtime, "build": {"material_normalization": normalization}}}
        return result

    def fixture(self):
        outer = self.root
        old = dict(self.metadata, target=str(outer / "main" / "source.blend"))
        self.metadata = old
        own = outer / "validator.py"
        before_bytes = b"sealed-original-validator"
        own.write_bytes(before_bytes)
        before = resume._file_record(own)
        current_code = Path(resume.__file__).read_text(encoding="utf-8")
        native, exported, prior, codes, sources, operations = [], [], [], [], [], []
        final_item = final_result = None
        for index in range(4):
            self.root = outer / ("main" if index == 3 else f"provider{index}")
            self.root.mkdir()
            path, digest, item, files = self.prepared(final=index == 3)
            if index == 3:
                final_result = self.normalized(item)
                final_item = item
            else:
                item["mesh_path"] = f"/Game/Tree/Custom{'ABC'[index]}"
            item["code_files"].append({"path": str(own), "size": len(before_bytes), "mtime_ns": 123,
                                       "fingerprint": hashlib.blake2b(before_bytes, digest_size=16).hexdigest()})
            item["fingerprint"] = resume._contract_fingerprint(item)
            report = resume._read(path)
            manifest_path = Path(report["manifest"])
            self.write(manifest_path, {"schema_version": 1, "items": [item],
                "checkpoint_path": report["checkpoint"], "report_path": report["batch_report"]})
            report["manifest_fingerprint"] = item["fingerprint"]
            report["prepared_rpc"] = resume.prepare_rpc_pause(report, manifest_path, item, files[5],
                source_paths=files[:3], final=index == 3)
            report_fp = self.write(path, report)
            if index < 3:
                self.content = outer / "Content"
                record = self.record(item["mesh_path"], owner="generated_skeletal_mesh", content=b"provider-earlier")
                record["sequence"] = 0
                state = {"status": "imported_ok", "durable_saves": {"schema_version": 1, "records": [record]},
                         "cluster_assembly": {"status": "skipped"}}
            else:
                state = final_result
            state["fingerprint"] = item["fingerprint"]
            batch = {"manifest": str(manifest_path), "status": "complete", "items": {item["queue_id"]: state}}
            checkpoint = {**batch, "current_item": None, "process_lifetime_policy": {"transport": "rpc"}}
            batch_fp = self.write(Path(report["batch_report"]), batch)
            checkpoint_fp = self.write(Path(report["checkpoint"]), checkpoint)
            imported_fp = self.write(Path(item["report_path"]), {**state, "queue_id": item["queue_id"], "checkpoint": report["checkpoint"]})
            native.append({"spm": str(files[1]), "final": index == 3, "export_report": report_fp,
                "manifest": resume._file_record(manifest_path), "queue_id": item["queue_id"], "fingerprint": item["fingerprint"],
                "checkpoint": checkpoint_fp, "batch_report": batch_fp, "item_import_report": imported_fp})
            exported.append({"spm": str(files[1]), "final": index == 3, "export_report": report_fp})
            if index < 3:
                wrapper = {"status": "ok", "stage": "completed", "unreal_result": state, "phase_completed": False,
                           "workstation_handoff": old, "export_report": report_fp, "manifest": str(manifest_path)}
                prior.append({"spm": str(files[1]), "final": False, "result": self.write(self.root / "resume.json", wrapper)})
            codes.append(resume._file_record(files[5]))
            sources.extend(report["prepared_rpc"]["source_files"])
            operations.extend([report_fp, resume._file_record(manifest_path), batch_fp, checkpoint_fp, imported_fp])
        # Main saves replace provider imports in the same exact package paths.
        for record in final_result["durable_saves"]["records"]:
            if record["package"].startswith("/Game/Tree/Custom"):
                Path(record["package_file"]).write_bytes(b"final-native")
                record.update(size=Path(record["package_file"]).stat().st_size, mtime_ns=Path(record["package_file"]).stat().st_mtime_ns)
        row = native[-1]
        report = resume._read(row["export_report"]["path"])
        batch = resume._read(row["batch_report"]["path"])
        batch["items"][row["queue_id"]] = final_result
        row["batch_report"] = self.write(Path(row["batch_report"]["path"]), batch)
        row["checkpoint"] = self.write(Path(row["checkpoint"]["path"]), {**batch, "current_item": None, "process_lifetime_policy": {"transport": "rpc"}})
        row["item_import_report"] = self.write(Path(row["item_import_report"]["path"]), {**final_result, "queue_id": row["queue_id"], "checkpoint": report["checkpoint"]})
        operations = [resume._file_record(record["path"]) for record in operations]
        failed_fp = self.write(outer / "failed.json", {"status": "failed_requires_reconciliation", "handoff": old, "imported": prior,
            "error": "Prepared RPC final save validation misses exact packages: /game/tree/referencea, /game/tree/referenceb"})
        export_fp = self.write(outer / "exports.json", {"status": "four_native_exports_pending_exact_checkout", "handoff": old, "exports": exported})
        current = dict(old, ticket_id="supported-new-ticket")
        live = {"read_only": True, "assets_saved": False, "ingest_repeated": False,
            "phase_id": current["phase_id"], "ticket_id": current["ticket_id"], "dirty_content": [], "dirty_maps": [],
            "native_probe_actors": [], "game_world": None, "native_runtime": final_result["cluster_assembly"]["runtime"],
            "dependency_policy": bend.dependency_policy(final_result["cluster_assembly"]["build"]["material_normalization"]),
            "native_item_result": row["item_import_report"],
            "native_readback": final_result["cluster_assembly"]["build"]["material_normalization"]["native_material_remap"]}
        readonly = []
        for package in ("/Game/Tree/ReferenceA", "/Game/Tree/ReferenceB"):
            record = self.record(package, content=b"untouched-reference")
            fp = resume._file_record(record["package_file"])
            readonly.append({"original": fp, "current": fp, "unchanged": True})
        stop = {"schema_version": 1, "status": "completed_native_rpc_stopped_final_save_validator_failed",
            "kind": "native_completed_bookkeeping_only", "handoff": old,
            "stop_receipt": {"schema_version": 1, "operation_stopped": True, "pending_native_operations": 0,
                "metadata": {k: old[k] for k in ("phase_id", "ticket_id", "provider", "session_id", "resource", "pipeline", "request_id", "target")}},
            "failed_import_ledger": failed_fp, "completed_export_ledger": export_fp, "frozen_code_files": [before, *codes],
            "source_fingerprints": sources, "preserved_history_files": [failed_fp], "operation_artifacts": operations,
            "native_exports": native, "durable_package_fingerprints": [resume._file_record(r["package_file"]) for r in final_result["durable_saves"]["records"]],
            "readonly_branch_files": readonly, "live_readback": self.write(outer / "live.json", live),
            "independent_native_audit": self.write(outer / "audit.json", {"read_only": True}), "validator_code_before": before,
            "validator_unchanged_ast_sha256": resume._completed_validator_ast_sha256(current_code),
            "allowed_ast_changed_function": "_validate_result_saves", "allowed_new_functions": ["_completed_validator_ast_sha256", "_completed_validator_context", "validate_completed_prepared_rpc", "reconcile_completed_prepared_rpc"],
            "actual_runtime_verified": True, "completed_providers": 3, "native_replay_authorized": False, "historical_files_rewritten": False}
        stop_path = outer / "stop.json"
        stop_fp = self.write(stop_path, stop)
        binding = self.write(outer / "binding.json", {"schema_version": 1, "previous": old, "current": current,
            "same_request_preserved": True, "actual_native_completed": True, "late_old_execution_rejected": True,
            "native_replay_authorized": False, "stop_receipt": stop["stop_receipt"], "source_stop_proof": stop_fp})
        own.write_text(current_code, encoding="utf-8")
        self.root = outer
        return {"stop_proof_path": stop_path, "expected_stop_proof_sha256": stop_fp["sha256"],
                "expected_validator_after_sha256": resume.file_sha256(own), "expected_handoff": current,
                "binding_reconciliation": binding}, own, stop

    def test_actual_normalized_scope_requires_mutations_but_not_readonly_role0(self):
        self.root = self.root / "main"
        self.root.mkdir()
        _, _, item, _ = self.prepared(final=True)
        result = self.normalized(item)
        validation = resume._validate_result_saves(item, result)
        self.assertEqual(validation["durable_package_count"], 10)
        self.assertNotIn("/Game/Tree/ReferenceA", validation["required_packages"])
        for remove in ("/Game/Tree/CustomA", "/Game/Tree/Base", result["durable_saves"]["records"][3]["package"]):
            bad = copy.deepcopy(result)
            bad["durable_saves"]["records"] = [r for r in bad["durable_saves"]["records"] if r["package"] != remove]
            for i, r in enumerate(bad["durable_saves"]["records"]): r["sequence"] = i
            with self.assertRaisesRegex(RuntimeError, "misses exact packages"):
                resume._validate_result_saves(item, bad)

    def test_role_preservation_remap_proof_or_legacy_absence_cannot_skip_parts(self):
        self.root = self.root / "main"
        self.root.mkdir()
        _, _, item, _ = self.prepared(final=True)
        result = self.normalized(item)
        changes = [lambda n: n["native_material_remap"].update(branch_materials_preserved=False),
                   lambda n: n["parts"][0].update(desired_remap=[5, 6]),
                   lambda n: n["normalized_part_bend_policy"]["contract"]["parts"][0].update(role=1),
                   lambda n: n.pop("normalized_part_bend_policy")]
        for change in changes:
            bad = copy.deepcopy(result)
            change(bad["cluster_assembly"]["build"]["material_normalization"])
            with self.assertRaises(RuntimeError): resume._validate_result_saves(item, bad)

    def test_completed_acknowledges_once_without_rpc_or_rewriting_failed_ledger(self):
        args, own, stop = self.fixture()
        before = Path(stop["failed_import_ledger"]["path"]).read_bytes()
        with mock.patch.object(resume, "__file__", str(own)):
            result = resume.reconcile_completed_prepared_rpc(**args, handoff_bridge=self.bridge)
        self.assertEqual((result["schema_version"], result["status"], result["stage"]), (2, "ok", "completed"))
        self.assertTrue(result["phase_completed"])
        self.assertFalse(result["native_replay_performed"])
        self.assertEqual(len(result["superseded_provider_packages"]), 3)
        self.assertEqual(result["provider_receipts_preserved"], 3)
        self.assertEqual(Path(stop["failed_import_ledger"]["path"]).read_bytes(), before)
        self.assertEqual(result["operation_handoff"], stop["handoff"])
        self.bridge.complete_handoff.assert_called_once()
        self.bridge.bind_handoff.assert_not_called()

    def test_existing_function_or_constant_ast_change_is_rejected_with_new_sha(self):
        for before, after in (("ITEM_ONLY_KEYS = {", "ITEM_ONLY_KEYS = {'unexpected',"),
                              ("def _runtime_passed(runtime):", "def _runtime_passed(other):")):
            with self.subTest(change=before):
                args, own, _ = self.fixture()
                own.write_text(own.read_text(encoding="utf-8").replace(before, after, 1), encoding="utf-8")
                args["expected_validator_after_sha256"] = resume.file_sha256(own)
                with mock.patch.object(resume, "__file__", str(own)), self.assertRaisesRegex(RuntimeError, "unapproved module code"):
                    resume.validate_completed_prepared_rpc(**args)
                self.temp.cleanup()
                self.setUp()
        self.bridge.complete_handoff.assert_not_called()

    def test_current_source_export_result_or_other_producer_change_rejects_completion(self):
        for group in ("source_fingerprints", "frozen_code_files", "operation_artifacts", "durable_package_fingerprints"):
            with self.subTest(group=group):
                args, own, stop = self.fixture()
                record = stop[group][-1]
                path = Path(record["path"])
                path.write_bytes(path.read_bytes() + b"changed")
                with mock.patch.object(resume, "__file__", str(own)), self.assertRaises(RuntimeError):
                    resume.reconcile_completed_prepared_rpc(**args, handoff_bridge=self.bridge)
                self.bridge.complete_handoff.assert_not_called()
                self.temp.cleanup()
                self.setUp()

    def test_request_change_or_missing_supported_ticket_proof_is_rejected(self):
        args, own, _ = self.fixture()
        for change in (lambda a: a.update(binding_reconciliation=None),
                       lambda a: a["expected_handoff"].update(request_id="another-request")):
            bad = copy.deepcopy(args)
            change(bad)
            with mock.patch.object(resume, "__file__", str(own)), self.assertRaises(RuntimeError):
                resume.reconcile_completed_prepared_rpc(**bad, handoff_bridge=self.bridge)
        self.bridge.complete_handoff.assert_not_called()

    def test_sealed_but_mismatched_checkpoint_is_not_completed(self):
        args, own, stop = self.fixture()
        checkpoint = stop["native_exports"][-1]["checkpoint"]
        value = resume._read(checkpoint["path"])
        key = stop["native_exports"][-1]["queue_id"]
        value["items"][key]["fingerprint"] = "another-native-item"
        changed = self.write(Path(checkpoint["path"]), value)
        stop["native_exports"][-1]["checkpoint"] = changed
        stop["operation_artifacts"] = [changed if row["path"] == changed["path"] else row for row in stop["operation_artifacts"]]
        stop_fp = self.write(args["stop_proof_path"], stop)
        args["expected_stop_proof_sha256"] = stop_fp["sha256"]
        binding = resume._read(args["binding_reconciliation"]["path"])
        binding["source_stop_proof"] = stop_fp
        args["binding_reconciliation"] = self.write(Path(args["binding_reconciliation"]["path"]), binding)
        with mock.patch.object(resume, "__file__", str(own)), self.assertRaisesRegex(RuntimeError, "checkpoint/batch/item"):
            resume.reconcile_completed_prepared_rpc(**args, handoff_bridge=self.bridge)
        self.bridge.complete_handoff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
