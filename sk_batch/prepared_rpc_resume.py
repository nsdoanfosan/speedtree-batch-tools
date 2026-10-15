"""Resume an immutable native Send2UE export in the existing editor.

The caller supplies the admitted handoff, the saved export-report digest and
an RPC sender accepting Python lines. This module starts no process, creates no
request and keeps the original export report immutable.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


ITEM_ONLY_KEYS = {
    "queue_id", "fingerprint", "send2ue_unreal_py", "wind_json",
    "checkout_asset_paths", "report_path", "export_report_path",
}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def file_sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _file_record(path):
    path = Path(path).resolve()
    return {"path": str(path), "size": path.stat().st_size, "sha256": file_sha256(path)}


def _contract_fingerprint(item):
    contract = {key: value for key, value in item.items() if key not in ITEM_ONLY_KEYS}
    encoded = json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def _validate_file(record):
    path = Path(record["path"])
    if record.get("exists") is False:
        if path.exists():
            raise RuntimeError("Previously absent prepared artifact now exists: " + str(path))
        return
    if not path.is_file():
        raise RuntimeError("Prepared artifact is missing: " + str(path))
    size = record.get("size", record.get("bytes"))
    if size is not None and path.stat().st_size != size:
        raise RuntimeError("Prepared artifact size changed: " + str(path))
    if record.get("sha256"):
        actual = file_sha256(path)
        expected = record["sha256"]
    elif record.get("fingerprint"):
        hasher = hashlib.blake2b(digest_size=16)
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        actual, expected = hasher.hexdigest(), record["fingerprint"]
    else:
        raise RuntimeError("Prepared artifact lacks a content digest: " + str(path))
    if actual != expected:
        raise RuntimeError("Prepared artifact digest changed: " + str(path))


def _current_input_paths(item):
    """Match canonical import's materialized inputs; provenance is diagnostic."""
    paths = {}
    sidecars = {}
    def add(value):
        if not value:
            return
        path = Path(value)
        if not path.is_file():
            raise RuntimeError("Prepared current import input is missing: " + str(path))
        paths.setdefault(_key(path), path)
    for name in ("exported_files", "handoff_files", "code_files"):
        for row in item.get(name) or []:
            add(row.get("path"))
            if name == "handoff_files" and str(row.get("path") or "").lower().endswith(".json"):
                sidecars.setdefault(_key(row["path"]), Path(row["path"]))
    add((item.get("wind_file") or {}).get("path"))
    payload = item.get("cluster_assembly") or {}
    plan = payload.get("ingest_plan") or {}
    for asset in [*(item.get("assets") or []), *(plan.get("assets") or [])]:
        data = asset.get("asset_data") or {}
        for key in ("_material_pipeline_json_path", "file_path", "fcurve_file_path"):
            add(data.get(key))
        sidecar = data.get("_material_pipeline_json_path") or (data.get("_material_pipeline_json_fingerprint") or {}).get("path")
        add(sidecar)
        if sidecar:
            sidecars.setdefault(_key(sidecar), Path(sidecar))
        for path in (data.get("lods") or {}).values():
            add(path)
    manifest = payload.get("manifest") or {}
    add((manifest.get("full_fbx") or {}).get("path"))
    add(((manifest.get("base") or {}).get("fbx") or {}).get("path"))
    add(((manifest.get("wind_contract") or {}).get("wind_json") or {}).get("path"))
    for part in manifest.get("parts") or []:
        external = part.get("external_source") or {}
        add(((external.get("plan_fbx") if external else part.get("fbx")) or {}).get("path"))
    for sidecar in sidecars.values():
        payload = _read(sidecar)
        for material in payload.get("materials") or []:
            # Match the native material importer: authored layers take priority
            # over the legacy entry-level texture array. It passes paths as-is.
            layers = material.get("layers") or [material]
            for layer in layers:
                for texture in layer.get("textures") or []:
                    file = texture.get("file")
                    if file and not Path(file).is_absolute():
                        raise RuntimeError("Prepared material texture path must be absolute: " + str(file))
                    add(file)
    return paths


def verify_deferred_export_receipt(receipt, metadata, blend_path):
    if (not isinstance(receipt, dict) or receipt.get("metadata") != metadata
            or _key(receipt.get("source_blend", "")) != _key(blend_path)
            or receipt.get("stage") != "exported_pending_unreal" or receipt.get("phase_completed") is not False
            or not isinstance(receipt.get("native_export_jobs"), int) or receipt["native_export_jobs"] <= 0
            or not receipt.get("exported_files")):
        raise RuntimeError("native Send2UE deferred export receipt does not match this exact Batch handoff")
    for row in receipt["exported_files"]:
        _validate_file(row)
    return receipt


def _runtime_passed(runtime):
    return bool(
        runtime.get("status") == "passed"
        and (runtime.get("begin") or {}).get("success") is True
        and (runtime.get("begin") or {}).get("status") == "pending"
        and (runtime.get("begin") or {}).get("probe_token")
        and (runtime.get("finish") or {}).get("success") is True
        and (runtime.get("finish") or {}).get("status") == "passed"
    )


def finish_batch_handoff(bridge, metadata, item_result, *, final):
    """Complete only after exact durable packages and runtime validation."""
    if metadata is None:
        return
    records = (item_result.get("durable_saves") or {}).get("records")
    assembly = item_result.get("cluster_assembly") or {}
    assembly_status = assembly.get("status")
    if (item_result.get("status") != "imported_ok" or not isinstance(records, list) or not records
            or any(record.get("saved") is not True or record.get("dirty_after_save") is not False for record in records)
            or (assembly_status not in (None, "skipped", "pass_through", "ok"))
            or (assembly_status == "ok" and not _runtime_passed(assembly.get("runtime") or {}))):
        raise RuntimeError("Batch handoff lacks clean durable packages and completed runtime validation")
    if not bridge.can_execute_handoff(metadata):
        raise RuntimeError("Batch handoff lost its active native request before completion")
    if final and not bridge.complete_handoff(metadata, metadata["request_id"], metadata["target"]):
        raise RuntimeError("Batch handoff final completion was not accepted for the exact native request")


def prepare_rpc_pause(report, manifest_path, item, runner_path, *, source_paths, final):
    """Seal the actual export outputs; called only after native export/manifest."""
    metadata = report.get("workstation_handoff")
    if report.get("transport") != "rpc" or not metadata:
        raise RuntimeError("Prepared RPC export requires its existing native handoff")
    verify_deferred_export_receipt(report.get("native_deferred_export_receipt"), metadata, item["blend"])
    if final:
        _require_final_main(item, metadata)
    current_paths = _current_input_paths(item)
    return {
        "schema_version": 1, "manifest": _file_record(manifest_path),
        "source_files": [_file_record(path) for path in source_paths],
        "runner_path": str(Path(runner_path).resolve()),
        "item_fingerprint": item["fingerprint"], "handoff": metadata,
        "final_handoff_requested": bool(final),
        "current_files": [_file_record(path) for path in current_paths.values()],
    }


def _require_final_main(item, metadata):
    payload = item.get("cluster_assembly") or {}
    manifest, plan = payload.get("manifest") or {}, payload.get("ingest_plan") or {}
    contract = plan.get("asset_contract") or {}
    if (_key(item.get("blend", "")) != _key(metadata.get("target", ""))
            or plan.get("status") != "ready" or not contract.get("assembly")
            or contract.get("full_skeletal_mesh") != item.get("mesh_path")
            or not manifest.get("parts")
            or manifest.get("full_asset_stem") != Path(item["blend"]).stem):
        raise RuntimeError("Only the exact main Assembly source may complete the shared handoff")


def _active_binding(bridge, metadata):
    fields = {"phase_id", "ticket_id", "provider", "session_id", "resource", "pipeline", "request_id", "target"}
    if (not fields.issubset(metadata) or any(not metadata[key] for key in fields)
            or metadata.get("pipeline") != "speedtree_batch_send2ue"
            or str(metadata.get("resource") or "").casefold() != "unreal:myproject2"
            or metadata.get("project") != "unreal:MyProject2"
            or metadata.get("action") != "send2ue_manifest_export_rpc_ingest"):
        raise RuntimeError("Prepared RPC handoff is not the exact native Batch request")
    phase = bridge.require_active_phase(metadata["phase_id"], "unreal:myproject2")
    if (phase.get("workload") != "heavy" or phase.get("provider") != metadata["provider"]
            or phase.get("session_id") != metadata["session_id"]):
        raise RuntimeError("Prepared RPC phase does not retain the admitted owner/heavy scope")
    bridge.require_scopes(metadata["phase_id"], ["editor"])
    if not bridge.can_execute_handoff(metadata) or not bridge.heartbeat_handoff(metadata):
        raise RuntimeError("Prepared RPC lost its active native request")


def validate_prepared_rpc(export_report_path, *, expected_handoff, expected_export_report_sha256):
    """Verify the caller's immutable report and every declared file before RPC."""
    export_report_path = Path(export_report_path).resolve()
    if not expected_export_report_sha256 or file_sha256(export_report_path) != expected_export_report_sha256:
        raise RuntimeError("Prepared export report digest changed")
    report = _read(export_report_path)
    prepared = report.get("prepared_rpc") or {}
    if (report.get("status") != "exported_pending_unreal" or report.get("transport") != "rpc"
            or report.get("send2ue") != "FINISHED" or prepared.get("schema_version") != 1
            or report.get("workstation_handoff") != expected_handoff or prepared.get("handoff") != expected_handoff):
        raise RuntimeError("Prepared export report does not retain the same native handoff")
    manifest_record = prepared.get("manifest") or {}
    if _key(manifest_record.get("path", "")) != _key(report.get("manifest", "")):
        raise RuntimeError("Prepared report identifies a different immutable manifest")
    _validate_file(manifest_record)
    manifest = _read(manifest_record["path"])
    items = manifest.get("items") or []
    if manifest.get("schema_version") != 1 or len(items) != 1:
        raise RuntimeError("Prepared RPC requires its one exact native export manifest item")
    item = items[0]
    if (item.get("workstation_handoff") != expected_handoff
            or item.get("workstation_phase_id") != expected_handoff.get("phase_id")
            or item.get("fingerprint") != prepared.get("item_fingerprint")
            or item.get("fingerprint") != report.get("manifest_fingerprint")
            or item.get("fingerprint") != _contract_fingerprint(item)
            or _key(item.get("export_report_path", "")) != _key(export_report_path)):
        raise RuntimeError("Prepared manifest item content or native binding changed")
    for name, key in (("checkpoint", "checkpoint_path"), ("batch_report", "report_path")):
        if _key(report.get(name, "")) != _key(manifest.get(key, "")):
            raise RuntimeError("Prepared manifest result path changed: " + name)
    sources = prepared.get("source_files") or []
    if len(sources) != 3 or _key(sources[0].get("path", "")) != _key(item.get("blend", "")):
        raise RuntimeError("Prepared export lacks its exact Blender/SPM/material source snapshots")
    for record in sources:
        _validate_file(record)
    if not item.get("exported_files") or not item.get("code_files"):
        raise RuntimeError("Prepared export lacks output/code fingerprints")
    runner = prepared.get("runner_path")
    if not runner or not any(_key(row.get("path", "")) == _key(runner) for row in item["code_files"]):
        raise RuntimeError("Prepared export lacks its exact native ingest implementation")
    current_files = prepared.get("current_files") or []
    for row in current_files:
        _validate_file(row)
    current_paths = _current_input_paths(item)
    if {_key(row["path"]) for row in current_files} != set(current_paths):
        raise RuntimeError("Prepared current file inventory changed")
    for name in ("exported_files", "handoff_files", "code_files"):
        for row in item.get(name) or []:
            _validate_file(row)
    if item.get("wind_file"):
        _validate_file(item["wind_file"])
    verify_deferred_export_receipt(report.get("native_deferred_export_receipt"), expected_handoff, item["blend"])
    return report, manifest, item


def runner_load_lines(runner_path):
    """Use the same shared ingest entrypoint as the normal Batch RPC worker."""
    return [
        "import importlib.util, sys",
        *[f"sys.modules.pop({name!r}, None)" for name in ("cluster_assembly_builder", "normalized_assembly_bend", "assembly_part_bend_policy", "nanite_assembly_materials", "assembly_part_notess", "preserved_assembly_carc")],
        f"_runner_path = {str(runner_path)!r}",
        "_spec = importlib.util.spec_from_file_location('sk_batch_unreal_ingest_rpc', _runner_path)",
        "if _spec is None or _spec.loader is None:",
        "\traise RuntimeError('Could not load SK Batch Unreal ingest: ' + _runner_path)",
        "_runner = importlib.util.module_from_spec(_spec)",
        "sys.modules[_spec.name] = _runner",
        "_spec.loader.exec_module(_runner)",
    ]


def _validate_result_saves(item, item_result):
    """Require imported/mutated packages; prove referenced role0 parts unchanged."""
    ledger = item_result.get("durable_saves") or {}
    records = ledger.get("records") or []
    if ledger.get("schema_version") != 1 or not records:
        raise RuntimeError("Prepared RPC lacks its native durable save ledger")
    packages = set()
    unchanged_references = []
    for index, record in enumerate(records):
        package = str(record.get("package") or "").split(".", 1)[0]
        file = Path(str(record.get("package_file") or ""))
        if (not package.startswith("/Game/") or package.casefold() in packages
                or record.get("sequence") != index or record.get("asset") != package
                or not record.get("owner") or not record.get("role")
                or record.get("saved") is not True or record.get("dirty_after_save") is not False
                or not file.is_file() or file.stat().st_size <= 0
                or file.stat().st_size != record.get("size") or file.stat().st_mtime_ns != record.get("mtime_ns")):
            raise RuntimeError("Prepared RPC final save validation failed: " + package)
        packages.add(package.casefold())
    required = {str(item.get("mesh_path") or "").split(".", 1)[0]}
    for asset in item.get("assets") or []:
        data = asset.get("asset_data") or {}
        if data.get("_asset_type") in ("SkeletalMesh", "StaticMesh"):
            required.add(str(data.get("asset_path") or "").split(".", 1)[0])
    plan = (item.get("cluster_assembly") or {}).get("ingest_plan") or {}
    if plan.get("status") == "ready":
        if ((item_result.get("cluster_assembly") or {}).get("status") != "ok"
                or not _runtime_passed((item_result.get("cluster_assembly") or {}).get("runtime") or {})):
            raise RuntimeError("Prepared RPC final save validation requires the actual passed editor runtime probe")
        contract = plan.get("asset_contract") or {}
        required.update(str(contract.get(name) or "").split(".", 1)[0]
                        for name in ("full_skeletal_mesh", "base_skeletal_mesh", "assembly"))
        normalization = ((item_result.get("cluster_assembly") or {}).get("build") or {}).get("material_normalization") or {}
        if "normalized_part_bend_policy" in normalization:
            from .normalized_assembly_bend import dependency_policy, plan_manifest_parts, unreal_asset_identity
            dependencies = dependency_policy(normalization)
            actual_parts = normalization.get("parts") or []
            planned = plan_manifest_parts((item.get("cluster_assembly") or {}).get("manifest") or {}, contract, actual_parts)
            native_contract = normalization["normalized_part_bend_policy"]["contract"]
            declared = native_contract.get("parts") or []
            native = normalization.get("native_material_remap") or {}
            if (unreal_asset_identity(normalization.get("assembly")) != unreal_asset_identity(contract.get("assembly"))
                    or len(declared) != len(planned) or native.get("part_count") != len(planned)
                    or normalization.get("dependency_save_status") not in ("persisted_verified_clean", "verified_clean")):
                raise RuntimeError("Prepared RPC normalized save scope differs from its exact Assembly")
            for expected, part, remap in zip(planned, declared, actual_parts):
                if (type(part.get("role")) is not int or part.get("index") != expected["index"]
                        or unreal_asset_identity(part.get("mesh")) != unreal_asset_identity(expected["mesh"])
                        or any(part.get(key) != expected[key] for key in ("role", "source_role", "bend_role"))
                        or remap.get("index") != expected["index"]
                        or (expected["role"] == 0 and remap.get("existing_remap") != remap.get("desired_remap"))):
                    raise RuntimeError("Prepared RPC normalized part role/remap preservation differs from manifest")
            checked = native.get("prototype_material_checks") or []
            if ({unreal_asset_identity(row.get("asset")) for row in checked}
                    != {unreal_asset_identity(path) for path in dependencies["prototypes"]}
                    or len(checked) != len(dependencies["prototypes"])):
                raise RuntimeError("Prepared RPC native prototype mutation scope differs from normalized ownership")
            required.update(dependencies["materials"])
            for row in checked:
                if type(row.get("changed")) is not bool:
                    raise RuntimeError("Prepared RPC prototype check lacks an exact mutation flag")
                if row["changed"]:
                    required.add(str(row["asset"]).split(".", 1)[0])
                    continue
                pairs = [("entire_mesh_description_sha1_before", "entire_mesh_description_sha1_after"),
                         ("protected_mesh_description_sha1_before", "protected_mesh_description_sha1_after"),
                         ("materials_authored_sha1_before", "materials_authored_sha1_expected"),
                         ("materials_authored_sha1_expected", "materials_authored_sha1_actual")]
                def exact_hash_pair(first, second):
                    values = [row.get(first), row.get(second)]
                    return all(isinstance(value, str) and len(value) == 40 and
                               all(c in "0123456789abcdefABCDEF" for c in value) for value in values) and values[0].casefold() == values[1].casefold()
                if (row.get("entire_mesh_description_preserved") is not True
                        or row.get("slot_names_and_other_authored_fields_preserved") is not True
                        or not all(exact_hash_pair(*pair) for pair in pairs)):
                    raise RuntimeError("Prepared RPC unchanged prototype lacks complete native preservation proof")
                unchanged_references.append(row["asset"])
        elif normalization.get("preserved_assembly_wind_policy"):
            from .preserved_assembly_carc import OWNER, validate_alias
            policy = normalization["preserved_assembly_wind_policy"]
            before, after = policy.get("prototype_streams_before"), policy.get("prototype_streams_after")
            expected = {str(p).split(".", 1)[0].casefold() for p in (contract.get("parts") or {}).values()}
            if (policy.get("owner") != OWNER or policy.get("verified") is not True or not policy.get("aliases")
                    or not isinstance(before, dict) or before != after
                    or {str(p).split(".", 1)[0].casefold() for p in before} != expected):
                raise RuntimeError("Prepared RPC authored C-arc preservation lacks exact unchanged references")
            for path, proof in policy["aliases"].items():
                validate_alias(path, proof)
            for path, proof in before.items():
                file = Path(str(proof.get("package_file") or ""))
                if (proof.get("live_clean") is not True or not file.is_file()
                        or file.stat().st_size != proof.get("package_size")
                        or file.stat().st_mtime_ns != proof.get("package_mtime_ns")
                        or hashlib.md5(file.read_bytes()).hexdigest() != proof.get("package_md5")):
                    raise RuntimeError("Prepared RPC authored C-arc prototype bytes changed: " + path)
                unchanged_references.append(path)
        else:
            required.update(str(path).split(".", 1)[0] for path in (contract.get("parts") or {}).values())
    missing = {path.casefold() for path in required if path} - packages
    if missing:
        raise RuntimeError("Prepared RPC final save validation misses exact packages: " + ", ".join(sorted(missing)))
    return {"required_packages": sorted(path for path in required if path), "durable_package_count": len(records),
            "unchanged_reference_packages": unchanged_references}


def _completed_validator_ast_sha256(source):
    """Seal all preexisting module code except the one reviewed save validator."""
    import ast
    tree = ast.parse(source)
    names = {"_validate_result_saves", "_completed_validator_ast_sha256", "_completed_validator_context",
             "validate_completed_prepared_rpc", "reconcile_completed_prepared_rpc"}
    tree.body = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names]
    return hashlib.sha256(ast.dump(tree, annotate_fields=True, include_attributes=False).encode("utf-8")).hexdigest()


def _completed_validator_context(stop_proof_path, expected_stop_proof_sha256, expected_validator_after_sha256,
                                 expected_handoff, binding_reconciliation):
    """Admit only sealed completed outputs and one explicit validator code delta."""
    proof_record = _file_record(stop_proof_path)
    if proof_record["sha256"] != expected_stop_proof_sha256:
        raise RuntimeError("Completed native stop proof digest changed")
    proof = _read(stop_proof_path)
    names = ["_completed_validator_ast_sha256", "_completed_validator_context",
             "validate_completed_prepared_rpc", "reconcile_completed_prepared_rpc"]
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or proof.get("status") != "completed_native_rpc_stopped_final_save_validator_failed"
            or proof.get("kind") != "native_completed_bookkeeping_only"
            or proof.get("actual_runtime_verified") is not True
            or proof.get("native_replay_authorized") is not False
            or proof.get("historical_files_rewritten") is not False
            or proof.get("allowed_ast_changed_function") != "_validate_result_saves"
            or proof.get("allowed_new_functions") != names):
        raise RuntimeError("Completed native proof does not authorize exact bookkeeping-only reconciliation")
    original = proof.get("handoff") or {}
    stopped = proof.get("stop_receipt") or {}
    metadata_fields = {"phase_id", "ticket_id", "provider", "session_id", "resource", "pipeline", "request_id", "target"}
    if (not metadata_fields.issubset(original) or any(not original[key] for key in metadata_fields)
            or type(stopped.get("schema_version")) is not int or stopped["schema_version"] != 1 or stopped.get("operation_stopped") is not True
            or stopped.get("pending_native_operations") != 0
            or stopped.get("metadata") != {key: original[key] for key in metadata_fields}):
        raise RuntimeError("Completed native operation was not safely stopped under its original request")
    binding_record = None
    if expected_handoff != original:
        if not binding_reconciliation:
            raise RuntimeError("Completed native ticket change lacks supported reconciliation")
        _validate_file(binding_reconciliation)
        binding_record = binding_reconciliation
        binding = _read(binding_record["path"])
        previous, current = binding.get("previous"), binding.get("current")
        if (type(binding.get("schema_version")) is not int or binding["schema_version"] != 1 or previous != original or current != expected_handoff
                or binding.get("same_request_preserved") is not True
                or binding.get("actual_native_completed") is not True
                or binding.get("late_old_execution_rejected") is not True
                or binding.get("native_replay_authorized") is not False
                or binding.get("stop_receipt") != stopped
                or _key((binding.get("source_stop_proof") or {}).get("path", "")) != _key(proof_record["path"])
                or (binding.get("source_stop_proof") or {}).get("sha256") != proof_record["sha256"]
                or set(previous) != set(current) or previous.get("ticket_id") == current.get("ticket_id")
                or any(previous[key] != current[key] for key in previous if key != "ticket_id")):
            raise RuntimeError("Completed native reconciliation changed more than the supported ticket binding")
    elif binding_reconciliation:
        raise RuntimeError("Completed native unchanged ticket must not supply a replacement binding")
    before = proof.get("validator_code_before") or {}
    own = Path(__file__).resolve()
    after = _file_record(own)
    if (_key(before.get("path", "")) != _key(own)
            or not expected_validator_after_sha256 or after["sha256"] != expected_validator_after_sha256
            or after["sha256"] == before.get("sha256")
            or _completed_validator_ast_sha256(own.read_text(encoding="utf-8")) != proof.get("validator_unchanged_ast_sha256")):
        raise RuntimeError("Completed native validator transition changed unapproved module code")
    fingerprinted_before = []
    for native in proof.get("native_exports") or []:
        _validate_file(native["export_report"])
        _validate_file(native["manifest"])
        sealed_report = _read(native["export_report"]["path"])
        sealed_item = _read(native["manifest"]["path"])["items"][0]
        sha_rows = [row for row in (sealed_report.get("prepared_rpc") or {}).get("current_files", [])
                    if _key(row.get("path", "")) == _key(own)]
        code_rows = [row for row in sealed_item.get("code_files") or [] if _key(row.get("path", "")) == _key(own)]
        if (len(sha_rows) != 1 or sha_rows[0].get("sha256") != before.get("sha256")
                or sha_rows[0].get("size") != before.get("bytes", before.get("size"))
                or len(code_rows) != 1 or code_rows[0].get("size") != sha_rows[0]["size"]
                or not isinstance(code_rows[0].get("fingerprint"), str)
                or len(code_rows[0]["fingerprint"]) != 32):
            raise RuntimeError("Completed export does not bind both original validator digest formats")
        fingerprinted_before.append(code_rows[0])
    if not fingerprinted_before or any(row != fingerprinted_before[0] for row in fingerprinted_before):
        raise RuntimeError("Completed exports do not seal one exact original validator generation")
    def validate_file(record):
        if _key(record.get("path", "")) == _key(own):
            if ((record.get("sha256") != before.get("sha256") and record != fingerprinted_before[0])
                    or record.get("size", record.get("bytes")) != before.get("size", before.get("bytes"))):
                raise RuntimeError("Completed manifest does not seal the exact original validator generation")
            return
        _validate_file(record)
    codes = proof.get("frozen_code_files") or []
    if (not codes or len({_key(row["path"]) for row in codes}) != len(codes)
            or sum(_key(row["path"]) == _key(own) for row in codes) != 1):
        raise RuntimeError("Completed native frozen implementation scope is malformed")
    for group in ("frozen_code_files", "source_fingerprints", "preserved_history_files", "operation_artifacts"):
        rows = proof.get(group)
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Completed native proof omitted immutable " + group)
        for row in rows:
            validate_file(row)
    for name in ("failed_import_ledger", "completed_export_ledger", "live_readback", "independent_native_audit"):
        _validate_file(proof.get(name) or {})
    for row in proof.get("durable_package_fingerprints") or []:
        _validate_file(row)
    return {"proof": proof, "proof_record": proof_record, "validator_before": before,
            "validator_after": after, "validate_file": validate_file, "binding_reconciliation": binding_record}


def validate_completed_prepared_rpc(stop_proof_path, *, expected_stop_proof_sha256,
                                    expected_validator_after_sha256, expected_handoff,
                                    binding_reconciliation=None):
    """Read completed native results without running export, ingest, or any RPC."""
    context = _completed_validator_context(stop_proof_path, expected_stop_proof_sha256,
        expected_validator_after_sha256, expected_handoff, binding_reconciliation)
    proof, original = context["proof"], context["proof"]["handoff"]
    exports = _read(proof["completed_export_ledger"]["path"])
    failed = _read(proof["failed_import_ledger"]["path"])
    native = proof.get("native_exports") or []
    if (exports.get("status") != "four_native_exports_pending_exact_checkout" or exports.get("handoff") != original
            or failed.get("status") != "failed_requires_reconciliation" or failed.get("handoff") != original
            or proof.get("completed_providers") != len(native) - 1 or len(native) < 2
            or len(exports.get("exports") or []) != len(native)
            or len(failed.get("imported") or []) != len(native) - 1
            or [row.get("final") for row in native] != [False] * (len(native) - 1) + [True]):
        raise RuntimeError("Completed native provider/final ledger sequence is not exact")
    # The existing validator body is unchanged. Its isolated globals recognize
    # only the explicitly sealed old validator file; normal resume stays strict.
    scope = dict(globals(), _validate_file=context["validate_file"])
    validate = type(validate_prepared_rpc)(validate_prepared_rpc.__code__, scope)
    results, items, superseded = [], [], []
    for index, (row, exported) in enumerate(zip(native, exports["exports"])):
        if row.get("spm") != exported.get("spm") or row.get("final") != exported.get("final") or row.get("export_report") != exported.get("export_report"):
            raise RuntimeError("Completed native export order/fingerprint changed")
        for name in ("export_report", "manifest", "checkpoint", "batch_report", "item_import_report"):
            _validate_file(row[name])
        report, manifest, item = validate(row["export_report"]["path"], expected_handoff=original,
            expected_export_report_sha256=row["export_report"]["sha256"])
        if (row.get("queue_id") != item.get("queue_id") or row.get("fingerprint") != item.get("fingerprint")
                or _key(row["manifest"]["path"]) != _key(report["manifest"])
                or _key(row["checkpoint"]["path"]) != _key(report["checkpoint"])
                or _key(row["batch_report"]["path"]) != _key(report["batch_report"])
                or _key(row["item_import_report"]["path"]) != _key(item["report_path"])):
            raise RuntimeError("Completed native result paths differ from the sealed manifest")
        batch, checkpoint = _read(report["batch_report"]), _read(report["checkpoint"])
        state = (batch.get("items") or {}).get(item["queue_id"])
        imported = _read(row["item_import_report"]["path"])
        if (not isinstance(state, dict) or state.get("status") != "imported_ok"
                or state.get("fingerprint") != item["fingerprint"]
                or (checkpoint.get("items") or {}).get(item["queue_id"]) != state
                or {key: value for key, value in imported.items() if key not in ("queue_id", "checkpoint")} != state
                or _key(batch.get("manifest", "")) != _key(report["manifest"])
                or _key(checkpoint.get("manifest", "")) != _key(report["manifest"])
                or (checkpoint.get("process_lifetime_policy") or {}).get("transport") != "rpc"
                or batch.get("status") != "complete" or checkpoint.get("current_item") is not None):
            raise RuntimeError("Completed native checkpoint/batch/item results do not match exactly")
        if index < len(native) - 1:
            prior = failed["imported"][index]
            _validate_file(prior["result"])
            saved = _read(prior["result"]["path"])
            if (prior.get("spm") != row["spm"] or prior.get("final") is not False
                    or saved.get("status") != "ok" or saved.get("stage") != "completed"
                    or saved.get("phase_completed") is not False or saved.get("unreal_result") != state
                    or saved.get("workstation_handoff") != original or saved.get("export_report") != _file_record(row["export_report"]["path"])):
                raise RuntimeError("Completed native provider receipt is not the prior successful import")
            records = (state.get("durable_saves") or {}).get("records") or []
            if ((state.get("durable_saves") or {}).get("schema_version") != 1 or not records
                    or any(record.get("saved") is not True or record.get("dirty_after_save") is not False
                           or record.get("sequence") != sequence or record.get("package") != record.get("asset")
                           for sequence, record in enumerate(records))):
                raise RuntimeError("Completed provider lacks its chronological durable saves")
            superseded.extend(records)
        results.append(state)
        items.append(item)
    final_item, final_result = items[-1], results[-1]
    _require_final_main(final_item, original)
    validation = _validate_result_saves(final_item, final_result)
    final_records = final_result["durable_saves"]["records"]
    latest = {_key(row["package_file"]): row for row in final_records}
    sealed_packages = proof.get("durable_package_fingerprints") or []
    if ({_key(row["path"]) for row in sealed_packages} != set(latest) or len(sealed_packages) != len(latest)
            or any(_key(row.get("package_file", "")) not in latest
                   or row.get("package", "").casefold() != latest[_key(row["package_file"])]["package"].casefold()
                   for row in superseded)):
        raise RuntimeError("Completed provider saves are not exactly superseded by sealed final saves")
    normalization = final_result["cluster_assembly"]["build"]["material_normalization"]
    from .normalized_assembly_bend import dependency_policy, require_native_proof, unreal_asset_identity
    policy = dependency_policy(normalization)
    prototypes = {unreal_asset_identity(path) for path in policy["prototypes"]}
    if ({unreal_asset_identity(row["package"]) for row in superseded} != prototypes or len(superseded) != len(prototypes)):
        raise RuntimeError("Completed provider supersession differs from normalized native prototype scope")
    parts = normalization["normalized_part_bend_policy"]["contract"]["parts"]
    readonly = {unreal_asset_identity(row["mesh"]) for row in parts if row["role"] == 0}
    branches = proof.get("readonly_branch_files") or []
    branch_packages = set()
    for row in branches:
        before, current = row.get("original") or {}, row.get("current") or {}
        _validate_file(current)
        if (row.get("unchanged") is not True or before != current):
            raise RuntimeError("Completed readonly prototype bytes changed")
        physical = str(current["path"]).replace("\\", "/").casefold()
        prefix, separator, relative = physical.rpartition("/content/")
        if not separator or not relative.endswith(".uasset"):
            raise RuntimeError("Completed readonly prototype lacks its actual Content package")
        branch_packages.add("/game/" + relative[:-7])
    if branch_packages != readonly or len(branches) != len(readonly):
        raise RuntimeError("Completed readonly branch byte proof differs from native role0 scope")
    missing_before = readonly - {unreal_asset_identity(row["package"]) for row in final_records}
    if failed.get("error") != "Prepared RPC final save validation misses exact packages: " + ", ".join(sorted(missing_before)):
        raise RuntimeError("Completed wrapper failure was not the exact readonly save-scope error")
    live = _read(proof["live_readback"]["path"])
    if (live.get("read_only") is not True or live.get("assets_saved") is not False
            or live.get("ingest_repeated") is not False or live.get("phase_id") != expected_handoff["phase_id"]
            or live.get("ticket_id") != expected_handoff["ticket_id"]
            or live.get("dirty_content") != [] or live.get("dirty_maps") != []
            or live.get("native_probe_actors") != [] or live.get("game_world") is not None
            or live.get("native_runtime") != final_result["cluster_assembly"]["runtime"]
            or live.get("dependency_policy") != policy
            or _key((live.get("native_item_result") or {}).get("path", "")) != _key(native[-1]["item_import_report"]["path"])
            or (live.get("native_item_result") or {}).get("sha256") != native[-1]["item_import_report"]["sha256"]):
        raise RuntimeError("Completed native live readback does not prove clean exact final scope")
    require_native_proof(live.get("native_readback"), parts, validate_only=True)
    reconciliation = {"stop_proof": context["proof_record"], "binding_proof": context["binding_reconciliation"],
                      "live_readback": proof["live_readback"], "independent_audit": proof["independent_native_audit"],
                      "operation_handoff": original, "completion_handoff": expected_handoff,
                      "native_replay_authorized": False}
    return {"schema_version": 2, "status": "completed_native_validated_without_replay", "stop_proof": context["proof_record"],
            "validator_before": context["validator_before"], "validator_after": context["validator_after"],
            "binding_reconciliation": context["binding_reconciliation"], "workstation_handoff": expected_handoff,
            "operation_handoff": original, "completion_reconciliation": reconciliation,
            "native_item_result": native[-1]["item_import_report"],
            "export_report": _file_record(native[-1]["export_report"]["path"]), "manifest": native[-1]["manifest"]["path"],
            "unreal_result": final_result, "final_save_validation": validation,
            "provider_receipts_preserved": len(results) - 1, "superseded_provider_packages": sorted(prototypes),
            "readonly_prototypes_preserved": sorted(readonly), "native_replay_performed": False,
            "failed_import_ledger_preserved": proof["failed_import_ledger"]}


def reconcile_completed_prepared_rpc(stop_proof_path, *, expected_stop_proof_sha256,
                                     expected_validator_after_sha256, expected_handoff,
                                     handoff_bridge, binding_reconciliation=None):
    """Acknowledge completed native work; never call an export or ingest sender."""
    result = validate_completed_prepared_rpc(stop_proof_path,
        expected_stop_proof_sha256=expected_stop_proof_sha256,
        expected_validator_after_sha256=expected_validator_after_sha256,
        expected_handoff=expected_handoff, binding_reconciliation=binding_reconciliation)
    _active_binding(handoff_bridge, expected_handoff)
    finish_batch_handoff(handoff_bridge, expected_handoff, result["unreal_result"], final=True)
    result.update(status="ok", stage="completed", phase_completed=True)
    return result


def resume_prepared_rpc(export_report_path, *, rpc_sender, handoff_bridge, expected_handoff,
                        expected_export_report_sha256, expected_source_fingerprints=(),
                        final=False, sleep=time.sleep):
    """Resume via caller-owned RPC; provider exports never complete the request."""
    if not callable(rpc_sender):
        raise TypeError("Prepared RPC requires a caller-provided existing-editor sender")
    report, manifest, item = validate_prepared_rpc(export_report_path,
        expected_handoff=expected_handoff, expected_export_report_sha256=expected_export_report_sha256)
    for record in expected_source_fingerprints:
        _validate_file(record)
    if bool(final) != report["prepared_rpc"].get("final_handoff_requested"):
        raise RuntimeError("Prepared provider/main final handoff intent differs")
    if final:
        _require_final_main(item, expected_handoff)
    bridge, metadata = handoff_bridge, expected_handoff
    _active_binding(bridge, metadata)
    bridge.require_scopes(metadata["phase_id"], ["source:" + Path(item["blend"]).as_posix()])
    runner = report["prepared_rpc"]["runner_path"]
    manifest_path, checkpoint_path, batch_path = report["manifest"], report["checkpoint"], report["batch_report"]
    queue_id = item["queue_id"]
    load_lines = runner_load_lines(runner)
    attempts = 0
    def send(lines):
        _active_binding(bridge, metadata)
        rpc_sender(load_lines + lines)
        _active_binding(bridge, metadata)
    def result():
        batch = _read(batch_path)
        checkpoint = _read(checkpoint_path)
        state = (batch.get("items") or {}).get(queue_id) or {}
        checkpoint_state = (checkpoint.get("items") or {}).get(queue_id) or {}
        if (state.get("fingerprint") != item["fingerprint"]
                or checkpoint_state != state
                or _key(batch.get("manifest", "")) != _key(manifest_path)
                or _key(checkpoint.get("manifest", "")) != _key(manifest_path)
                or (checkpoint.get("process_lifetime_policy") or {}).get("transport") != "rpc"):
            raise RuntimeError("RPC result does not prove this exact manifest in the existing editor")
        return state
    try:
        send([f"_runner.run_manifest({manifest_path!r}, checkpoint_path={checkpoint_path!r}, report_path={batch_path!r})"])
        item_result = result()
        while item_result.get("status") == "runtime_pending" and attempts < 30:
            sleep(0.1)
            attempts += 1
            send([f"_runner.finish_runtime_probe({checkpoint_path!r}, {batch_path!r}, {queue_id!r})"])
            item_result = result()
        if item_result.get("status") == "runtime_pending":
            reason = "runtime probe did not complete within 30 cross-frame RPC attempts"
            send([f"_runner.cancel_runtime_probe({checkpoint_path!r}, {batch_path!r}, {queue_id!r}, {reason!r})"])
            raise RuntimeError(reason)
        validate_prepared_rpc(export_report_path, expected_handoff=metadata,
            expected_export_report_sha256=expected_export_report_sha256)
        for record in expected_source_fingerprints:
            _validate_file(record)
        _validate_result_saves(item, item_result)
        finish_batch_handoff(bridge, metadata, item_result, final=bool(final))
        return {"status": "ok", "stage": "completed", "unreal_result": item_result,
                "export_report": _file_record(export_report_path), "manifest": manifest_path,
                "runtime_finish_attempts": attempts, "workstation_handoff": metadata,
                "phase_completed": bool(final)}
    except BaseException as error:
        bridge.fail_handoff(metadata, str(error)[:2000])
        raise
