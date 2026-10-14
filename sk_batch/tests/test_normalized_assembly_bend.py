import hashlib
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))
import normalized_assembly_bend as bend
from sk_batch.tests.test_unreal_ingest import load_runner
from cluster_assembly_builder import ClusterAssemblyBuildError, _production_assembly_ui_receipt


ROOT = "/Game/Meshes/02_nature/Tree/Tree_Weeping_Willow/Assembly/SK_Willow_NaniteAssembly"


def leaf(index, role=1):
    return {"index": index, "mesh": f"/Game/Meshes/02_nature/Tree/Tree_Weeping_Willow/Cluster/SK_leaf_{index:02d}",
            "role": role, "source_sha1": "a" * 40, "source_file": "C:/Exports/leaf.fbx"}


def native_proof(parts):
    return {"schema_version": 2, "operation": "configure_normalized_assembly_bend_native", "ok": True,
        "payload_checked_prototype_count": len([part for part in parts if part["role"]]),
        "payload_verified_before_material_mutation": True, "production_prototype_materials_directly_applied": True,
        "root_source_mesh_description_preserved": True, "branch_materials_preserved": True,
        "uv3_verifications": [{"prototype": part["mesh"], "role": part["role"],
            "import_file_sha1": part["source_sha1"], "native_import_md5_matches": True,
            "verified": True, "uv_channel_count": 4, "aspect_ratio": 4.0,
            "correction_factor": 2.0, "analytical_pivot_mask": 0.0}
            for part in parts if part["role"]]}


class ManifestAndProofTests(unittest.TestCase):
    @staticmethod
    def active_fixture():
        parts = [{"index": index, "mesh": f"/Game/Any/Part{index}", "role": 0 if index < 2 else 2 if index > 6 else 1}
                 for index in range(9)]
        actual = [{"mesh": part["mesh"], "remap": [0, 1] if part["role"] == 0 else [1, 4]} for part in parts]
        return parts, actual

    def test_numeric_role_union_keeps_original_five_slots_and_excludes_disabled_parts(self):
        parts, actual = self.active_fixture()
        before = deepcopy((parts, actual))
        self.assertEqual(bend.plan_active_source_indices(5, parts, actual, 5), [1, 4])
        self.assertEqual((parts, actual), before)
        # Capture/source names have no authority over the independent numeric role.
        parts[2]["role"] = 0
        actual[2]["remap"] = [3]
        self.assertEqual(bend.plan_active_source_indices(5, parts, actual, 5), [1, 4])

    def test_compact_and_legacy_remaps_decode_same_active_sources_without_modulo(self):
        parts, actual = self.active_fixture()
        for row, part in zip(actual, parts):
            if part["role"]:
                row["remap"] = [5, 6]
        self.assertEqual(bend.plan_active_source_indices(5, parts, actual, 7, {"active_source_indices": [1, 4]}), [1, 4])
        self.assertNotEqual([5 % 5, 6 % 5], [1, 4])
        for row, part in zip(actual, parts):
            if part["role"]:
                row["remap"] = [6, 9]
        self.assertEqual(bend.plan_active_source_indices(5, parts, actual, 10, {"schema_version": 2}), [1, 4])

    def test_invalid_compact_table_role_order_remaps_or_prior_indices_reject(self):
        parts, actual = self.active_fixture()
        for previous in ({"active_source_indices": [4, 1]}, {"active_source_indices": [1, 1]},
                         {"active_source_indices": [True, 4]}, {"active_source_indices": [1, 5]}):
            with self.assertRaises(bend.NormalizedAssemblyBendError):
                bend.plan_active_source_indices(5, parts, actual, 7, previous)
        for mutate in (lambda p, a: a[2].update(remap=[10]), lambda p, a: p[2].update(role=True),
                       lambda p, a: p[2].update(index=1), lambda p, a: a[0].update(remap=[6]),
                       lambda p, a: a[3].update(mesh="/Game/Other/Part")):
            p, a = deepcopy((parts, actual))
            mutate(p, a)
            with self.assertRaises(bend.NormalizedAssemblyBendError):
                bend.plan_active_source_indices(5, p, a, 10)
        with self.assertRaises(bend.NormalizedAssemblyBendError):
            bend.plan_active_source_indices(5, parts, actual, 7)
        with self.assertRaises(bend.NormalizedAssemblyBendError):
            bend.plan_active_source_indices(5, parts, actual, 10, {"active_source_indices": [1, 4]})

    def test_compact_dependency_scope_allows_only_active_variants_and_complete_compile_closure(self):
        parts = [leaf(index) for index in range(7)]
        sources = [f"/Game/Materials/MI_Source{index}" for index in range(5)]
        variants = [bend.variant_path(path) if index in (1, 4) else path for index, path in enumerate(sources)]
        packages = [bend.variant_path("/Game/Materials/M_Bark", master=True),
                    bend.variant_path("/Game/Materials/M_Atlas", master=True), variants[1], variants[4]]
        report = {"assembly": ROOT, "apply_requested": True,
            "native_material_remap": {**native_proof(parts), "active_source_indices": [1, 4], "material_count": 7},
            "normalized_part_bend_policy": {"aspect_policy": bend.ASPECT_POLICY, "material_packages": packages,
                "contract": {"schema_version": 2, "assembly": ROOT, "parts": parts, "source_materials": sources,
                             "active_source_indices": [1, 4], "variant_materials": variants}},
            "material_compile_proofs": [{"asset": path, "native_finish_compilation_waited": True,
                "shader_count": 1, "recompile_errors": []} for path in packages]}
        self.assertEqual(bend.dependency_policy(report)["materials"], packages)
        for mutate in (lambda r: r["normalized_part_bend_policy"]["contract"]["variant_materials"].__setitem__(0, bend.variant_path(sources[0])),
                       lambda r: r["native_material_remap"].update(active_source_indices=[0, 4]),
                       lambda r: r["native_material_remap"].update(material_count=10),
                       lambda r: r["normalized_part_bend_policy"]["material_packages"].remove(variants[4])):
            bad = deepcopy(report)
            mutate(bad)
            with self.assertRaises(bend.NormalizedAssemblyBendError):
                bend.dependency_policy(bad)

    @staticmethod
    def cluster_manifest():
        mapping = {f"part_{index}": f"/Game/Trees/Cluster/SK_cluster_reed_01_{index:02d}" for index in range(1, 5)}
        manifest = {"parts": [{"prototype_id": key, "role": "cluster", "external_source": {
            "kind": "send_to_unreal_normalized_skeletal_part",
            "pivot_contract": "normalized_attachment_origin_0_0_0"}} for key in mapping]}
        return manifest, {"parts": mapping}, [{"mesh": path} for path in mapping.values()]

    def test_four_normalized_clusters_keep_semantics_and_use_ordinary_bend_code(self):
        manifest, contract, actual = self.cluster_manifest()
        before = deepcopy(manifest)
        parts = bend.plan_manifest_parts(manifest, contract, actual)
        self.assertEqual(manifest, before)
        self.assertEqual([part["source_role"] for part in parts], ["cluster"] * 4)
        self.assertEqual([part["role"] for part in parts], [1] * 4)
        self.assertEqual([part["index"] for part in parts], [0, 1, 2, 3])
        for part in parts:
            part.update(source_sha1="a" * 40, source_file="C:/Exports/cluster.fbx")
        bend.require_native_proof(native_proof(parts), parts)
        bad = native_proof(parts)
        bad["uv3_verifications"][3]["native_import_md5_matches"] = False
        with self.assertRaises(bend.NormalizedAssemblyBendError):
            bend.require_native_proof(bad, parts)

    def test_cluster_raw_source_or_wrong_pivot_is_rejected_before_app_calls(self):
        manifest, contract, actual = self.cluster_manifest()
        for source in ({"kind": "raw_source"},
                       {"kind": "send_to_unreal_normalized_skeletal_part", "pivot_contract": "centered_bbox"},
                       {"kind": "send_to_unreal_normalized_skeletal_part"}):
            bad = deepcopy(manifest)
            bad["parts"][0]["external_source"] = source
            with self.assertRaises(bend.NormalizedAssemblyBendError):
                bend.plan_manifest_parts(bad, contract, actual)
        manifest["parts"][0]["role"] = "branch"
        manifest["parts"][0]["external_source"] = {"kind": "raw_source"}
        parts = bend.plan_manifest_parts(manifest, contract, actual)
        self.assertEqual((parts[0]["source_role"], parts[0]["role"]), ("branch", 0))
        self.assertEqual([part["role"] for part in parts[1:]], [1, 1, 1])

    def test_nested_production_ui_receipt_is_exact_and_never_inferred(self):
        target = str(Path(tempfile.gettempdir()) / "AssemblySource.blend")
        receipt = {"path": target, "saved": True, "ui_preserved": True, "save_version_restored": True,
                   "preferences_persisted": False, "ui_sha256": "a" * 64}
        line = "PRODUCTION_ASSEMBLY_UI=" + json.dumps(receipt)
        self.assertEqual(_production_assembly_ui_receipt("Blender log\n" + line, target), receipt)
        for output in ("Saved production file", line + "\n" + line,
                       "PRODUCTION_ASSEMBLY_UI=" + json.dumps({**receipt, "path": target + "_other"}),
                       "PRODUCTION_ASSEMBLY_UI=" + json.dumps({**receipt, "ui_preserved": False})):
            with self.assertRaises(ClusterAssemblyBuildError):
                _production_assembly_ui_receipt(output, target)

    def test_manifest_roles_deduplicate_without_species_heuristics(self):
        mapping = {"branch": "/Game/Trees/Branch", "long": "/Game/Trees/AnySpecies", "side": "/Game/Trees/Side"}
        source = {"kind": "send_to_unreal_normalized_skeletal_part"}
        manifest = {"parts": [{"prototype_id": "branch", "role": "branch"},
            {"prototype_id": "long", "role": "leaf", "external_source": source},
            {"prototype_id": "long", "role": "leaf", "external_source": source},
            {"prototype_id": "side", "role": "leaf_side", "external_source": source}]}
        actual = [{"mesh": path} for path in mapping.values()]
        parts = bend.plan_manifest_parts(manifest, {"parts": mapping}, actual)
        self.assertEqual([part["role"] for part in parts], [0, 1, 2])
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "order"):
            bend.plan_manifest_parts(manifest, {"parts": mapping}, list(reversed(actual)))
        manifest["parts"][2]["role"] = "leaf_side"
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "conflicting"):
            bend.plan_manifest_parts(manifest, {"parts": mapping}, actual)

    def test_case_aliases_deduplicate_roles_but_keep_actual_native_paths_and_order(self):
        mapping = {"branch": "/Game/Trees/Cluster/SK_Branch", "leaf": "/Game/Trees/Cluster/SK_Custom",
                   "alias": "/game/Trees/cluster/sk_custom", "side": "/Game/Trees/Cluster/SK_Side"}
        source = {"kind": "send_to_unreal_normalized_skeletal_part"}
        manifest = {"parts": [{"prototype_id": "branch", "role": "branch"},
            {"prototype_id": "leaf", "role": "leaf", "external_source": source},
            {"prototype_id": "alias", "role": "leaf", "external_source": source},
            {"prototype_id": "side", "role": "leaf_side", "external_source": source}]}
        actual = [{"mesh": "/Game/Trees/cluster/SK_Branch.SK_Branch"},
                  {"mesh": "/Game/Trees/cluster/sk_custom.sk_custom"},
                  {"mesh": "/Game/Trees/cluster/SK_Side.SK_Side"}]
        before = deepcopy((manifest, mapping, actual))
        parts = bend.plan_manifest_parts(manifest, {"parts": mapping}, actual)
        self.assertEqual([row["mesh"] for row in parts], [bend.package_path(row["mesh"]) for row in actual])
        self.assertEqual([row["role"] for row in parts], [0, 1, 2])
        self.assertEqual([row["index"] for row in parts], [0, 1, 2])
        self.assertEqual((manifest, mapping, actual), before)
        for bad in (list(reversed(actual)), actual + [actual[1]], [{"mesh": "/Game/Trees/SK_Unrelated"}, *actual[1:]]):
            with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "order"):
                bend.plan_manifest_parts(manifest, {"parts": mapping}, bad)
        manifest["parts"][2]["role"] = "leaf_side"
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "conflicting"):
            bend.plan_manifest_parts(manifest, {"parts": mapping}, actual)

    def test_case_only_uv_proof_paths_keep_exact_role_hash_and_pivot_guards(self):
        parts = [leaf(index, 2 if index >= 5 else 1) for index in range(7)]
        report = native_proof(parts)
        for proof in report["uv3_verifications"]:
            proof["prototype"] = proof["prototype"].replace("/Cluster/", "/cluster/").replace("SK_leaf", "sk_leaf")
        bend.require_native_proof(report, parts)
        for change in ({"prototype": "/Game/Trees/SK_Unrelated"}, {"role": 0},
                       {"import_file_sha1": "b" * 40}, {"native_import_md5_matches": False},
                       {"analytical_pivot_mask": 0.1}):
            bad = deepcopy(report)
            bad["uv3_verifications"][2].update(change)
            with self.assertRaises(bend.NormalizedAssemblyBendError):
                bend.require_native_proof(bad, parts)

    def test_loaded_native_identity_precedes_uv_validation_and_keeps_serialized_contract(self):
        class Mesh:
            def __init__(self, path, values):
                self.path, self.values = path, values
            def get_path_name(self):
                return self.path + "." + self.path.rsplit("/", 1)[-1]
            def get_editor_property(self, name):
                return self.values[name]

        with tempfile.TemporaryDirectory() as directory:
            fbx = Path(directory) / "sk_custom.fbx"
            fbx.write_bytes(b"exact-current-source-generation")
            assembly_path, mesh_path = "/Game/Trees/Assembly/SK_Any", "/Game/Trees/Cluster/SK_Custom"
            native_mesh_path = "/Game/Trees/cluster/sk_custom"
            source_material = SimpleNamespace(get_path_name=lambda: "/Game/M/MI_Source.MI_Source")
            source_slot = SimpleNamespace(get_editor_property=lambda name: source_material)
            prototype = Mesh(mesh_path, {"asset_import_data": SimpleNamespace(extract_filenames=lambda: [str(fbx)])})
            assembly = Mesh(assembly_path, {"nanite_settings": SimpleNamespace(export_text=lambda: "native-original-text"),
                                          "materials": [source_slot]})
            function = object()
            calls = []
            def native(path, text):
                contract = json.loads(text)
                calls.append(contract)
                return json.dumps({**native_proof(contract["parts"]),
                    "active_source_indices": contract["active_source_indices"],
                    "planned_material_count": len(contract["source_materials"]) + len(contract["active_source_indices"])})
            registry = {bend.unreal_asset_identity(path): asset for path, asset in
                        [(assembly_path, assembly), (mesh_path, prototype), (bend.FUNCTION, function)]}
            unreal = SimpleNamespace(SkeletalMesh=Mesh,
                EditorAssetLibrary=SimpleNamespace(load_asset=lambda path: registry.get(bend.unreal_asset_identity(path)),
                    get_metadata_tag=lambda asset, tag: bend.ASPECT_POLICY if asset is function and tag == bend.ASPECT_TAG else ""),
                EditorLoadingAndSavingUtils=SimpleNamespace(get_dirty_content_packages=lambda: []),
                CodexAssemblyPartBendUvLibrary=SimpleNamespace(configure_normalized_assembly_bend=native))
            manifest = {"parts": [{"prototype_id": "custom", "role": "leaf",
                                   "external_source": {"kind": "send_to_unreal_normalized_skeletal_part"}}]}
            contract = {"assembly": assembly_path.replace("/Assembly/", "/assembly/"), "parts": {"custom": mesh_path}}
            def run():
                return bend.normalize_assembly(unreal, assembly, manifest, contract, apply=False, allow_dirty=False,
                    parse_parts=lambda text: [{"index": 0, "mesh": native_mesh_path + ".sk_custom", "remap": [0]}],
                    native_json=json.loads, slot_record=lambda value, index: {}, audit=lambda *args: {"status": "ok"})
            result = run()
            self.assertEqual(calls[0]["parts"][0]["mesh"], native_mesh_path)
            self.assertEqual(calls[0]["parts"][0]["source_sha1"], hashlib.sha1(fbx.read_bytes()).hexdigest())
            self.assertEqual(result["parts"][0]["mesh"], native_mesh_path + ".sk_custom")
            calls.clear()
            impostor = Mesh(mesh_path, prototype.values)
            original_load = unreal.EditorAssetLibrary.load_asset
            unreal.EditorAssetLibrary.load_asset = lambda path: impostor if path == native_mesh_path else original_load(path)
            with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "same SkeletalMesh"):
                run()
            self.assertEqual(calls, [])
            self.assertEqual(fbx.read_bytes(), b"exact-current-source-generation")

    def test_seven_willow_prototypes_require_all_exact_native_imports(self):
        parts = [leaf(index, 2 if index >= 5 else 1) for index in range(7)]
        report = native_proof(parts)
        bend.require_native_proof(report, parts)
        for mutation in (lambda value: value["uv3_verifications"].pop(),
                         lambda value: value["uv3_verifications"][4].update(native_import_md5_matches=False),
                         lambda value: value["uv3_verifications"][1].update(correction_factor=1.0),
                         lambda value: value.update(schema_version=1)):
            bad = deepcopy(report)
            mutation(bad)
            with self.assertRaises(bend.NormalizedAssemblyBendError):
                bend.require_native_proof(bad, parts)

    def test_protected_base_fields_keep_nested_and_ripple_state(self):
        source = '(OpacityMaskClipValue=0.25,BlendMode=BLEND_Masked,Nested=(Text="a,b"),bOverride_MaxWorldPositionOffsetDisplacement=False,MaxWorldPositionOffsetDisplacement=0)'
        changed = source.replace("Displacement=False", "Displacement=True").replace("Displacement=0)", "Displacement=300)")
        self.assertEqual(bend.protected_base_fields(source), bend.protected_base_fields(changed))
        self.assertNotEqual(bend.protected_base_fields(source), bend.protected_base_fields(changed.replace("BLEND_Masked", "BLEND_Opaque")))

    def test_generated_paths_include_full_source_identity(self):
        self.assertNotEqual(bend.variant_path("/Game/A/MI_Leaf"), bend.variant_path("/Game/B/MI_Leaf"))
        self.assertNotEqual(bend.variant_path("/Game/A/MI_Leaf"), bend.variant_path("/Game/A/MI_Leaf", master=True))


class CheckoutTests(unittest.TestCase):
    def test_manual_scope_reuses_only_matching_pending_cl_and_rejects_default(self):
        paths = [bend.variant_path("/Game/A/MI_Leaf")]
        mappings = [{"depotFile": "//Depot/AssemblyBend/MI_Leaf.uasset"}]
        forms, calls = {}, []
        def p4(*args, form=None):
            calls.append(args)
            if args[0] == "changes":
                return [{"change": number, "desc": value["Description"]} for number, value in forms.items()]
            if args[:2] == ("change", "-i"):
                forms["900"] = {"Status": "pending", "Client": "UnrealProjects", "Description": form[b"Description"].decode("utf-8")}
                return [{"data": "Change 900 created."}]
            return [forms[str(args[-1])]]
        first = bend.manual_material_changelist(paths, mappings, [], p4=p4)
        forms["900"]["Files0"] = mappings[0]["depotFile"] + "#1"
        second = bend.manual_material_changelist(paths, mappings, [{"action": "edit", "change": "900"}], p4=p4)
        self.assertEqual((first, second), (900, 900))
        self.assertEqual(sum(call[:2] == ("change", "-i") for call in calls), 1)
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "default"):
            bend.manual_material_changelist(paths, mappings, [{"action": "edit", "change": "default"}], p4=p4)
        forms["900"]["Files1"] = "//Depot/Unrelated/User.uasset#2"
        with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "unrelated"):
            bend.manual_material_changelist(paths, mappings, [], p4=p4)

    def test_mapping_conflict_and_open_cl_are_verified_before_graph_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            path = bend.variant_path("/Game/A/MI_Leaf")
            file = content.joinpath(*path[6:].split("/")).with_suffix(".uasset")
            file.parent.mkdir(parents=True)
            file.write_bytes(b"submitted-material")
            calls, opened = [], []
            record = {"clientFile": str(file), "headRev": "1", "digest": hashlib.md5(file.read_bytes()).hexdigest()}
            unreal = SimpleNamespace(Paths=SimpleNamespace(project_content_dir=lambda: str(content), convert_relative_path_to_full=lambda value: value))
            def p4(*args):
                calls.append(args)
                if args[0] == "change":
                    return [{"Status": "pending", "Client": "UnrealProjects"}]
                if args[0] == "where":
                    return [{"path": str(file)}]
                if args[0] == "edit":
                    opened.append(2374)
                    return [{"action": "edit"}]
                return [{**record, **({"action": "edit", "change": opened[-1]} if opened else {})}]
            result = bend.checkout_material_scope(unreal, [path], 2374, p4=p4)
            self.assertTrue(result["verified"])
            self.assertEqual(opened, [2374])
            self.assertEqual(calls[-1][0], "fstat")
            opened[:] = [9999]
            calls.clear()
            with self.assertRaisesRegex(bend.NormalizedAssemblyBendError, "conflict"):
                bend.checkout_material_scope(unreal, [path], 2374, p4=p4)
            self.assertNotIn("edit", [call[0] for call in calls])


class NormalizedDependencySaveTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_runner()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        content = Path(self.temporary.name)
        self.parts = [leaf(index, 2 if index >= 5 else 1) for index in range(7)]
        self.materials = [bend.variant_path("/Game/A/M_Leaf", master=True), bend.variant_path("/Game/A/MI_Leaf")]
        self.prototypes = [part["mesh"] for part in self.parts]
        self.paths = self.materials + self.prototypes
        self.dirty, self.saved, self.fail_at = set(), [], None
        self.assets = {path: SimpleNamespace(path=path) for path in self.paths}
        self.runner.unreal.Paths = SimpleNamespace(project_content_dir=lambda: str(content), convert_relative_path_to_full=lambda value: value)
        self.runner.unreal.EditorAssetLibrary = SimpleNamespace(load_asset=lambda path: self.assets.get(path))
        self.runner.unreal.EditorLoadingAndSavingUtils = SimpleNamespace(get_dirty_content_packages=lambda: [SimpleNamespace(get_path_name=lambda path=path: path) for path in self.dirty])
        def save(asset):
            self.saved.append(asset.path)
            if len(self.saved) == self.fail_at:
                return False
            file = self.runner._durable_package_file(asset.path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"normalized-material-delivery")
            self.dirty.discard(asset.path)
            return True
        self.runner.unreal.CodexMaterialToolsLibrary = SimpleNamespace(save_asset_package_without_thumbnail=save)
        self.runner._refresh_headless_saved_asset_registry = lambda path: None
        self.ledger = self.runner._new_durable_save_ledger()
        for path in self.prototypes:
            file = self.runner._durable_package_file(path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"provider-prebuild")
            self.runner._record_durable_save(self.ledger, path, owner="assembly_prototype_prebuild", role="prototype", save_mode="thumbnail_free")
        self.item = {"checkout_asset_paths": [ROOT] + self.prototypes, "workstation_phase_id": "p_test"}
        self.frozen = {"verified": True, "phase_id": "p_test", "assembly": ROOT,
                       "materials": self.materials, "prototypes": self.prototypes, "changelist": 2374}
        self.runner._NORMALIZED_BEND_DEPENDENCY_SCOPES[id(self.item)] = deepcopy(self.frozen)
        self.report = {"assembly": ROOT, "apply_requested": True, "dependent_packages_to_save": self.paths,
            "dependency_checkout": deepcopy(self.frozen), "native_material_remap": native_proof(self.parts),
            "normalized_part_bend_policy": {"aspect_policy": bend.ASPECT_POLICY,
                "contract": {"schema_version": 2, "assembly": ROOT, "parts": self.parts,
                             "variant_materials": [self.materials[1]]}, "material_packages": self.materials},
            "material_compile_proofs": [{"asset": path, "native_finish_compilation_waited": True, "shader_count": 1, "recompile_errors": []} for path in self.materials]}
        self.phase_guard = patch.object(bend, "require_active_dependency_phase", return_value={"workload": "heavy"})
        self.phase_guard.start()
        self.addCleanup(self.phase_guard.stop)

    def test_materials_then_seven_prototypes_saved_with_original_owner(self):
        self.dirty.update(self.paths)
        self.dirty.add("/Game/Unrelated/Unsaved")
        result = self.runner._save_normalized_part_bend_dependencies(self.item, self.report, durable_saves=self.ledger)
        self.assertEqual(self.saved, self.paths)
        self.assertEqual(result["status"], "persisted_verified_clean")
        self.assertEqual(self.dirty, {"/Game/Unrelated/Unsaved"})
        for path in self.prototypes:
            record = self.runner._find_durable_save(self.ledger, path)
            self.assertEqual(record["owner"], "assembly_prototype_prebuild")
            self.assertEqual(len(record["postbuild_refreshes"]), 1)
        self.assertTrue(self.runner._validate_durable_save_ledger(self.ledger))

    def test_missing_shader_or_unrelated_scope_prevents_every_save(self):
        self.dirty.update(self.paths)
        self.report["material_compile_proofs"][0]["shader_count"] = 0
        with self.assertRaisesRegex(RuntimeError, "compilation"):
            self.runner._save_normalized_part_bend_dependencies(self.item, self.report, durable_saves=self.ledger)
        self.report["material_compile_proofs"][0]["shader_count"] = 1
        self.report["dependent_packages_to_save"] = self.paths + ["/Game/Unrelated/Unsaved"]
        with self.assertRaisesRegex(RuntimeError, "unrelated"):
            self.runner._save_normalized_part_bend_dependencies(self.item, self.report, durable_saves=self.ledger)
        self.assertEqual(self.saved, [])

    def test_dependency_failure_prevents_root_audit_and_save(self):
        self.fail_at = 2
        root_calls = []
        def build(*args, **kwargs):
            self.dirty.update(self.paths)
            return {"assembly": ROOT, "material_normalization": self.report}
        self.runner.build_unreal_nanite_assembly = build
        self.runner.validate_manifest_artifacts = lambda manifest: None
        self.runner._material_postbuild_slot_audit = lambda path: root_calls.append("audit")
        self.runner._save_large_assembly_without_thumbnail = lambda *args, **kwargs: root_calls.append("save")
        self.item["cluster_assembly"] = {"manifest": {"parts": [{"role": "leaf"}]}, "ingest_plan": {"status": "ready"}}
        with self.assertRaisesRegex(RuntimeError, "failed to persist normalized"):
            self.runner._finalize_prepared_cluster_assembly(self.item, {"status": "prepared_for_build"}, durable_saves=self.ledger)
        self.assertEqual(root_calls, [])
        self.assertEqual(self.saved, self.materials)
        self.assertIn(self.materials[1], self.dirty)

    def test_cluster_only_manifest_installs_dependency_callback_before_build(self):
        callbacks = []
        notess = []
        self.runner._finalize_assembly_part_notess = lambda _item, path, **_kwargs: notess.append(path) or {"status": "verified_part_notess", "save_writability": []}
        self.runner.validate_manifest_artifacts = lambda manifest: None
        self.runner._prepare_normalized_bend_dependency_scope = lambda item, plan: callbacks.append((item, plan))
        def build(_unreal, _manifest, _contract, **kwargs):
            self.assertTrue(callable(kwargs.get("prepare_bend_dependencies")))
            kwargs["prepare_bend_dependencies"]({"assembly": ROOT})
            return {"assembly": ROOT}
        self.runner.build_unreal_nanite_assembly = build
        self.runner._ensure_declared_package_writable = lambda item, path: {"asset": path}
        self.runner._material_postbuild_slot_audit = lambda path: {"status": "ok"}
        self.runner._save_large_assembly_without_thumbnail = lambda *args, **kwargs: ROOT
        self.item["cluster_assembly"] = {"manifest": {"parts": [{"role": "cluster"}]}, "ingest_plan": {"status": "ready"}}
        result = self.runner._finalize_prepared_cluster_assembly(self.item, {"status": "prepared_for_build"}, durable_saves=self.ledger)
        self.assertEqual(notess, [ROOT])
        self.assertEqual(callbacks, [(self.item, {"assembly": ROOT})])
        self.assertEqual(result["status"], "ready_for_runtime")

    def test_independent_bend_metadata_controls_dependency_callback(self):
        from sk_batch.tests.test_explicit_bend_manifest import manifest_part, normalized_contract
        notess = []
        self.runner._finalize_assembly_part_notess = lambda _item, path, **_kwargs: notess.append(path) or {"status": "verified_part_notess", "save_writability": []}
        self.runner.validate_manifest_artifacts = lambda manifest: None
        self.runner._ensure_declared_package_writable = lambda item, path: {"asset": path}
        self.runner._material_postbuild_slot_audit = lambda path: {"status": "ok"}
        self.runner._save_large_assembly_without_thumbnail = lambda *args, **kwargs: ROOT
        for capture_role, bend_role, expected in (("branch", "cluster", True), ("leaf", "NONE", False)):
            def build(_unreal, _manifest, _contract, **kwargs):
                self.assertEqual(callable(kwargs.get("prepare_bend_dependencies")), expected)
                return {"assembly": ROOT}
            self.runner.build_unreal_nanite_assembly = build
            part = manifest_part(normalized_contract(["SK_CustomFlow_01"], None if bend_role == "NONE" else bend_role),
                "SK_CustomFlow_01", capture_role=capture_role)
            self.item["cluster_assembly"] = {"manifest": {"parts": [part]}, "ingest_plan": {"status": "ready",
                "asset_contract": {"parts": {part["prototype_id"]: "/Game/Trees/Cluster/SK_CustomFlow_01"}}}}
            self.runner._finalize_prepared_cluster_assembly(self.item,
                {"status": "prepared_for_build"}, durable_saves=self.ledger)
        self.assertEqual(notess, [ROOT, ROOT])  # Includes the explicit NONE wind role.

    def test_invalid_independent_metadata_fails_ingest_before_assembly_build(self):
        from sk_batch.tests.test_explicit_bend_manifest import manifest_part, normalized_contract
        builds = []
        self.runner.validate_manifest_artifacts = lambda manifest: None
        self.runner.build_unreal_nanite_assembly = lambda *args, **kwargs: builds.append(args)
        part = manifest_part(normalized_contract(["SK_CustomFlow_01"], None), "SK_CustomFlow_01", capture_role="branch")
        for mutate in (lambda source: source.update(part_bend_role="unknown"),
                       lambda source: source.pop("part_bend_policy"),
                       lambda source: source.update(part_bend_unknown=True),
                       lambda source: source["part_bend_source_proof"].update(schema_version=1)):
            bad = deepcopy(part)
            mutate(bad["external_source"])
            self.item["cluster_assembly"] = {"manifest": {"parts": [bad]}, "ingest_plan": {"status": "ready",
                "asset_contract": {"parts": {bad["prototype_id"]: "/Game/Trees/Cluster/SK_CustomFlow_01"}}}}
            with self.assertRaises(bend.NormalizedAssemblyBendError):
                self.runner._finalize_prepared_cluster_assembly(self.item,
                    {"status": "prepared_for_build"}, durable_saves=self.ledger)
        self.assertEqual(builds, [])


if __name__ == "__main__":
    unittest.main()
