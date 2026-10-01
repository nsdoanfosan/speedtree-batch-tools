"""Queue identity, publication lifecycle, and exact Perforce scope boundaries.

Every application/process/Perforce boundary is mocked. Temporary files are
small synthetic receipts; no real Blender source, editor, or depot is touched.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest import mock

from sk_batch import debris_prefab_pipeline as pipeline
from sk_batch.jobs import debris_prefab_ingest_job as ingest_job


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def mesh_item(queue_id, mesh, *, extra=None):
    assets = [{"asset_data": {"_asset_type": "SkeletalMesh", "asset_path": mesh}}]
    if extra:
        assets.append({"asset_data": {"_asset_type": "StaticMesh", "asset_path": extra}})
    return {"queue_id": queue_id, "mesh_path": mesh, "assets": assets}


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = write(self.root / "manifest.json", {"items": [
            mesh_item("source-A", "/Game/Arbitrary/SK_A", extra="/Game/Arbitrary/SM_A"),
            mesh_item("source-B", "/Game/Different/SK_B")]})

    def receipt(self, **values):
        return write(self.root / "push.json", {"status": "imported_ok", "manifest": str(self.manifest), **values})

    def test_multi_item_queue_identity_selects_only_its_skeletal_source(self):
        receipt = self.receipt(queue_id="source-B")
        self.assertEqual(pipeline.source_mesh_from_push(receipt), "/Game/Different/SK_B")
        # An unrelated global mesh_path cannot change the queue-owned identity.
        receipt = self.receipt(queue_id="source-B", mesh_path="/Game/Arbitrary/SK_A")
        self.assertEqual(pipeline.source_mesh_from_push(receipt), "/Game/Different/SK_B")

    def test_missing_or_duplicate_queue_identity_cannot_fall_back_to_other_item(self):
        with self.assertRaises(pipeline.DebrisPrefabPipelineError):
            pipeline.source_mesh_from_push(self.receipt(queue_id="absent", mesh_path="/Game/Arbitrary/SK_A"))
        write(self.manifest, {"items": [mesh_item("duplicate", "/Game/A/SK_A"),
                                       mesh_item("duplicate", "/Game/B/SK_B")]})
        with self.assertRaises(pipeline.DebrisPrefabPipelineError):
            pipeline.source_mesh_from_push(self.receipt(queue_id="duplicate"))

    def test_ambiguous_manifest_needs_queue_or_explicit_exported_identity(self):
        with self.assertRaises(pipeline.DebrisPrefabPipelineError):
            pipeline.source_mesh_from_push(self.receipt())
        self.assertEqual(pipeline.source_mesh_from_push(self.receipt(mesh_path="/Game/Arbitrary/SK_A")),
                         "/Game/Arbitrary/SK_A")

    def test_unfinished_push_cannot_author_prefab(self):
        for status in ("running", "exported_pending_unreal", "failed", None):
            with self.subTest(status=status), self.assertRaises(pipeline.DebrisPrefabPipelineError):
                pipeline.source_mesh_from_push(self.receipt(status=status, queue_id="source-A"))


class PipelineLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "Project/Fixture.uproject"
        self.blender = self.root / "fake-blender.exe"
        self.spm = self.root / "Art/Ground_cover_A.spm"
        self.blend = self.spm.with_suffix(".blend")
        for path in (self.project, self.blender, self.spm, self.blend,
                     self.project.parent / "Scripts/PCG/debris_prefab_pipeline.py",
                     self.project.parent / "Scripts/PCGTests/dump_debris_group_expected.py",
                     self.project.parent / "Scripts/PCGTests/verify_debris_group_import.py"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic fixture; never executable")
        self.original = write(self.root / "original_push.json", {"status": "ok", "mesh_path": "/Game/Source/SK_A"})
        self.logs = self.root / "logs"
        self.key = hashlib.sha256(str(self.spm.resolve()).casefold().encode("utf-8")).hexdigest()[:16]
        self.folder = self.logs / "debris_prefab" / self.key
        self.options = dict(blender=self.blender, unreal_project=self.project,
                            material_contract=self.root / "material.json", log_dir=self.logs,
                            source_push_report=self.original, transport="rpc")
        self.executed = []
        self.authoring_status = "ok"

    def export(self, command, **kwargs):
        self.executed.append(command)
        self.assertNotIn("--save-groups", command)
        self.assertEqual(command[command.index("--transport") + 1], "headless_export")
        return SimpleNamespace(returncode=0)

    def native(self, *args, **kwargs):
        request = json.loads((self.folder / "request.json").read_text(encoding="utf-8"))
        write(Path(request["paths"]["authoring"]), {"status": self.authoring_status, "success": True,
              "group_count": 3, "changelist": 123, "import_geometry_verified": True,
              "original_fields_preserved": True, "guard_closed": {"success": True}})
        return {"success": True}

    def run_mocked(self, **overrides):
        options = {**self.options, **overrides}
        with mock.patch.object(pipeline, "owned_run", side_effect=self.export), \
             mock.patch.object(pipeline, "_remote_call", side_effect=self.native), \
             mock.patch.object(pipeline, "send2ue_export_cache_root", return_value=self.root / "export-cache"):
            return pipeline.run_pipeline(self.spm, **options)

    def test_complete_lifecycle_reports_only_native_completion_and_preserves_originals(self):
        original = self.original.read_bytes()
        blend = self.blend.read_bytes()
        result = self.run_mocked()
        persisted = json.loads((self.folder / "pipeline.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(persisted["stage"], "completed")
        self.assertEqual(result["group_count"], 3)
        self.assertFalse(result["source_saved"])
        self.assertTrue(result["original_push_preserved"])
        self.assertEqual(self.original.read_bytes(), original)
        self.assertEqual(self.blend.read_bytes(), blend)

    def test_native_incomplete_state_is_failed_and_keeps_material_error_receipt(self):
        for status in ("running", "failed", "exported_pending_unreal"):
            with self.subTest(status=status):
                self.authoring_status = status
                with self.assertRaises(pipeline.DebrisPrefabPipelineError) as caught:
                    self.run_mocked(changelist=123)
                report = json.loads((self.folder / "pipeline.json").read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["authoring"]["status"], status)
                self.assertEqual(caught.exception.report["status"], "failed")

    def test_cancel_before_export_has_no_process_or_native_side_effect(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(pipeline.DebrisPrefabPipelineError):
            self.run_mocked(cancel_event=event)
        self.assertEqual(self.executed, [])
        self.assertEqual(json.loads((self.folder / "pipeline.json").read_text())["status"], "failed")

    def test_reexecution_reuses_pending_own_cl_but_exports_latest_source_and_receipt(self):
        first = self.run_mocked()
        write(self.folder / "expected/expected_group_geometry.json", {"stale": True})
        write(self.folder / "native/import_geometry.json", {"stale": True})
        self.blend.write_bytes(b"new canonical saved source bytes")
        write(self.original, {"status": "imported_ok", "mesh_path": "/Game/Renamed/SK_Latest"})
        original_latest = self.original.read_bytes()
        with mock.patch.object(ingest_job, "_p4", return_value=[{"Status": "pending", "Client": "UnrealProjects"}]):
            second = self.run_mocked()
        request = json.loads((self.folder / "request.json").read_text())
        self.assertEqual(request["changelist"], first["changelist"])
        self.assertEqual(second["source_skeletal_mesh"], "/Game/Renamed/SK_Latest")
        self.assertEqual(len(self.executed), 2)
        self.assertFalse((self.folder / "expected/expected_group_geometry.json").exists())
        self.assertFalse((self.folder / "native/import_geometry.json").exists())
        self.assertEqual(self.blend.read_bytes(), b"new canonical saved source bytes")
        self.assertEqual(self.original.read_bytes(), original_latest)


class PerforceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "Fixture.uproject"
        self.assets = ["/Game/Derived/SM_A", "/Game/Derived/BP_A"]
        self.files = [self.project.parent / "Content/Derived/SM_A.uasset",
                      self.project.parent / "Content/Derived/BP_A.uasset"]
        self.calls = []

    def records(self, operation, *args, **kwargs):
        self.calls.append((operation, args, kwargs))
        if operation == "where":
            return [{"path": str(p), "clientFile": "//UnrealProjects/Content/Derived/" + p.name}
                    for p in self.files]
        if operation == "fstat":
            return [{"clientFile": str(p), "headRev": "1"} for p in self.files]
        if operation == "change":
            return [{"Status": "pending", "Client": "UnrealProjects"}]
        return []

    def test_other_changelist_rejects_before_creation_edit_or_add(self):
        def response(op, *args, **kwargs):
            if op == "fstat":
                return [{"clientFile": str(self.files[0]), "headRev": "1", "action": "edit", "change": "999"}]
            return self.records(op, *args, **kwargs)
        with mock.patch.object(ingest_job, "_p4", side_effect=response):
            with self.assertRaisesRegex(RuntimeError, "changelist conflict"):
                ingest_job._checkout_scope(self.project, self.assets, None)
        self.assertFalse(any(op in {"edit", "add"} or (op == "change" and "-i" in args)
                             for op, args, kwargs in self.calls))

    def test_same_count_wrong_or_duplicate_native_mappings_are_rejected(self):
        for mappings in ([{"path": str(self.files[0])}] * 2,
                         [{"path": str(p.with_name("Wrong" + p.name))} for p in self.files]):
            self.calls.clear()
            def response(op, *args, **kwargs):
                if op == "where":
                    return mappings
                return self.records(op, *args, **kwargs)
            with self.subTest(mappings=mappings), mock.patch.object(ingest_job, "_p4", side_effect=response):
                with self.assertRaises(RuntimeError):
                    ingest_job._checkout_scope(self.project, self.assets, 123)
            self.assertFalse(any(op in {"edit", "add"} for op, args, kwargs in self.calls))

    def test_supplied_changelist_must_be_pending_in_the_exact_client(self):
        for status, client in (("submitted", "UnrealProjects"), ("pending", "ArtSources")):
            self.calls.clear()
            def response(op, *args, **kwargs):
                if op == "change":
                    self.calls.append((op, args, kwargs))
                    return [{"Status": status, "Client": client}]
                return self.records(op, *args, **kwargs)
            with self.subTest(status=status, client=client), mock.patch.object(ingest_job, "_p4", side_effect=response):
                with self.assertRaises(RuntimeError):
                    ingest_job._checkout_scope(self.project, self.assets, 123)
            self.assertFalse(any(op in {"edit", "add"} for op, args, kwargs in self.calls))

    def test_existing_valid_numbered_changelist_is_reused_with_exact_checkout_scope(self):
        with mock.patch.object(ingest_job, "_p4", side_effect=self.records):
            cl, assets, files = ingest_job._checkout_scope(self.project, self.assets, 123)
        self.assertEqual(cl, 123)
        self.assertEqual(assets, self.assets)
        self.assertEqual(files, self.files)
        edits = [args for op, args, kwargs in self.calls if op == "edit"]
        self.assertEqual(edits, [("-c", "123", *(str(p) for p in self.files))])
        self.assertFalse(any(op == "change" and "-i" in args for op, args, kwargs in self.calls))


class NativeStrictStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {key: self.root / (key + ".json") for key in
                      ("payload", "preflight", "authoring", "manifest", "checkpoint", "ingest", "geometry_comparison", "verified")}
        self.request = write(self.root / "request.json", {"project": str(self.root / "Fixture.uproject"),
                     "repo": str(self.root), "paths": {k: str(v) for k, v in self.paths.items()},
                     "expected_dir": str(self.root / "expected"), "native_dir": str(self.root / "native"),
                     "verification_python": "mock-python", "changelist": 123})
        self.editor = SimpleNamespace(preflight=mock.Mock(return_value={"checkout_asset_paths": ["/Game/Derived/SM_A"]}),
                                      dump_geometry=mock.Mock(), finalize=mock.Mock())
        self.native = SimpleNamespace(run_manifest=mock.Mock(return_value={"items": {"one": {"status": "imported_ok"}}}))
        self.library = SimpleNamespace(begin_native_authoring_guard=mock.Mock(return_value=json.dumps({"success": True, "token": "guard"})),
                                       end_native_authoring_guard=mock.Mock(return_value=json.dumps({"success": True})))
        self.unreal = SimpleNamespace(load_asset=mock.Mock(return_value=object()), CodexDebrisPrefabEditorLibrary=self.library)

    def run_with_mocks(self, proof_returncode=1, scope_files=()):
        with mock.patch.dict("sys.modules", {"unreal": self.unreal}), \
             mock.patch.object(ingest_job, "_load", side_effect=lambda name, path: self.editor if name == "debris_editor_pipeline" else self.native), \
             mock.patch.object(ingest_job, "_checkout_scope", return_value=(123, ["/Game/Derived/SM_A"], list(scope_files))), \
             mock.patch.object(ingest_job.subprocess, "run", return_value=SimpleNamespace(returncode=proof_returncode, stdout="strict proof failed")):
            return ingest_job.run(str(self.request))

    def test_incomplete_static_ingest_never_dumps_or_authors_and_always_closes_guard(self):
        self.native.run_manifest.return_value = {"items": {"one": {"status": "data_error"}}}
        with self.assertRaisesRegex(RuntimeError, "ingest did not complete"):
            self.run_with_mocks()
        self.editor.dump_geometry.assert_not_called()
        self.editor.finalize.assert_not_called()
        self.library.end_native_authoring_guard.assert_called_once()
        self.assertEqual(json.loads(self.paths["authoring"].read_text())["status"], "failed")

    def test_failed_independent_geometry_proof_never_authors_or_binds(self):
        with self.assertRaisesRegex(RuntimeError, "geometry verification failed"):
            self.run_with_mocks()
        self.editor.dump_geometry.assert_called_once()
        self.editor.finalize.assert_not_called()
        self.library.end_native_authoring_guard.assert_called_once()
        receipt = json.loads(self.paths["authoring"].read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["source_wind_modified"])
        self.assertFalse(receipt["map_saved"])
        self.assertFalse(receipt["pcg_generated"])

    def test_guard_close_failure_cannot_report_completed_publication(self):
        self.library.end_native_authoring_guard.return_value = json.dumps({"success": False})
        self.editor.preflight.return_value["material_bindings"] = []
        self.editor.finalize.return_value = {"status": "ok", "success": True, "changelist": 123, "group_count": 3}
        write(self.paths["payload"], {"synthetic_payload": True})
        write(self.paths["geometry_comparison"], {"synthetic_strict_proof": True})
        write(self.root / "expected/expected_group_geometry.json", {"header": "expected"})
        write(self.root / "native/import_geometry.json", {"header": "native"})
        with mock.patch("sk_batch.debris_prefab_manifest.finalize_import_verified_manifest",
                        return_value={"import_geometry_verified": True}):
            result = self.run_with_mocks(proof_returncode=0)
        self.editor.finalize.assert_called_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("guard", result["error"])
        self.assertEqual(json.loads(self.paths["authoring"].read_text())["status"], "failed")

    def test_failed_or_ambiguous_finalize_preserves_partial_ledger_and_never_registers_assets(self):
        self.editor.preflight.return_value["material_bindings"] = []
        write(self.paths["payload"], {"synthetic_payload": True})
        write(self.paths["geometry_comparison"], {"synthetic_strict_proof": True})
        write(self.root / "expected/expected_group_geometry.json", {"header": "expected"})
        write(self.root / "native/import_geometry.json", {"header": "native"})
        generated = self.root / "Content/Derived/SM_A.uasset"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"synthetic generated asset")
        ledger = {"completed_meshes": ["/Game/Derived/SM_A"], "failed_stage": "dataasset_assignment"}
        for status, success in (("failed", False), ("ok", False), ("failed", True), ("ok", None), ("ok", 1)):
            self.editor.finalize.return_value = {"status": status, "success": success,
                                                "changelist": 123, "partial_ledger": ledger}
            with self.subTest(status=status, success=success), \
                 mock.patch("sk_batch.debris_prefab_manifest.finalize_import_verified_manifest",
                            return_value={"import_geometry_verified": True}), \
                 mock.patch.object(ingest_job, "_p4", return_value=[]) as p4:
                with self.assertRaises(RuntimeError):
                    self.run_with_mocks(proof_returncode=0, scope_files=[generated])
            self.assertFalse(any(call.args and call.args[0] == "add" for call in p4.call_args_list))
            receipt = json.loads(self.paths["authoring"].read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["partial_ledger"], ledger)
            self.assertTrue(receipt["guard_closed"]["success"])


if __name__ == "__main__":
    unittest.main()
