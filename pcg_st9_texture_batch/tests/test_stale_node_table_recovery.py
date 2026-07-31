"""Race and provenance regressions for stale Node-table recovery."""

import contextlib
import gzip
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pcg_cluster_assembly_contract import (  # noqa: E402
    _stale_node_table_recovery_contract,
)
from speedtree_pipeline_contract import (  # noqa: E402
    spm_authoring_graph_fingerprint,
)
from stale_node_table_recovery import (  # noqa: E402
    RECOVERY_CONTRACT,
    StaleNodeTableRecoveryError,
    StaleNodeTableRecoveryTimeout,
    _acquire_session_lock,
    _authoring_graph_core_projection,
    _capture_immutable_snapshot,
    _ensure_preimage_artifacts,
    _legacy_authoring_graph_core_v2_projection,
    _legacy_authoring_graph_core_v3_projection,
    _legacy_target_binding_fingerprint,
    _release_session_lock,
    _resolve_receipt_dialect,
    _resolve_target_scopes,
    build_parser,
    recover_stale_node_table,
    verify_sealed_resave,
)


TARGET_MESH_IDS = (130, 131, 132, 133)
MINTED_GENERATOR_GUID = "RegressionFixtureGuidA=="
MODELER_GENERATOR_GUID = "RegressionFixtureGuid=="


def _node(guid):
    return (
        "<Node>"
        f"<GeneratorGUID>{guid}</GeneratorGUID>"
        "<ParentGUID></ParentGUID><Name>node</Name><GUID>node-guid</GUID>"
        "<Hidden>false</Hidden>"
        "<Extra><m_bDeleted>false</m_bDeleted><m_bCulled>false</m_bCulled></Extra>"
        "</Node>"
    )


def spm_text(
    *,
    stale,
    graph_property="1",
    link_source="root-guid",
    mesh_name_suffix="",
    volatile="one",
    missing_target_node=None,
    material_by_mesh=None,
):
    material_by_mesh = material_by_mesh or {}
    generators = [
        "<Generator Type=\"Tree\"><Name>Tree</Name><GUID>root-guid</GUID>"
        "<Hidden>false</Hidden><Properties></Properties></Generator>"
    ]
    links = []
    meshes = []
    for mesh_id in TARGET_MESH_IDS:
        material_id = material_by_mesh.get(mesh_id, 10)
        generators.append(
            "<Generator Type=\"Frond\">"
            f"<Name>Leaf {mesh_id}</Name><GUID>g-{mesh_id}</GUID>"
            "<Hidden>false</Hidden><Properties>"
            "<Property><Name>Leaf:Material</Name>"
            f"<Value>{material_id}</Value></Property>"
            f"<Property><Name>Leaf:Mesh</Name><Value>{mesh_id}</Value></Property>"
            "<Property><Name>Custom:Density</Name>"
            f"<Value>{graph_property}</Value></Property>"
            "</Properties></Generator>"
        )
        links.append(
            "<Link>"
            f"<SourceGUID>{link_source}</SourceGUID>"
            f"<TargetGUID>g-{mesh_id}</TargetGUID>"
            "</Link>"
        )
        meshes.append(
            f"<Mesh ID=\"{mesh_id}\"><Name>mesh-{mesh_id}{mesh_name_suffix}</Name></Mesh>"
        )
    if stale:
        nodes = [_node("orphan-guid") for _ in TARGET_MESH_IDS]
    else:
        nodes = [
            _node(f"g-{mesh_id}")
            for mesh_id in TARGET_MESH_IDS
            if mesh_id != missing_target_node
        ]
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<SpeedTree>"
        f"<Thumbnail>{volatile}</Thumbnail><Preview>{volatile}</Preview>"
        f"<QuickSaveSettings2>{volatile}</QuickSaveSettings2>"
        f"<m_sTimelineData>{volatile}</m_sTimelineData>"
        f"<Generators>{''.join(generators)}</Generators>"
        f"<Links>{''.join(links)}</Links>"
        f"<Assets><Meshes>{''.join(meshes)}</Meshes></Assets>"
        f"<Nodes>{''.join(nodes)}</Nodes>"
        "</SpeedTree>"
    )


def authored_scope_text(
    *,
    stale,
    guid_suffix,
    volatile,
    root_values=None,
    material_filename="leaf_a.png",
):
    root_values = root_values or {}
    blocks = []
    for tag in ("Force", "RuleScript", "Fan", "Light"):
        value = root_values.get(tag, "1")
        blocks.append(
            f"<{tag}><GUID>{tag.casefold()}-{guid_suffix}</GUID><Properties>"
            f"<Property><Name>{tag}:Authored</Name><Value>{value}</Value>"
            f"</Property></Properties></{tag}>"
        )
    material = (
        '<Material_V8 ID="10"><Preview>'
        f"{volatile}</Preview><StreamPlaceholder><Data>{volatile}</Data>"
        "</StreamPlaceholder><Map Name=\"Color\"><TexFilename>"
        f"{material_filename}</TexFilename><TexEnabled>true</TexEnabled>"
        "</Map><CutoutMeshID>130</CutoutMeshID></Material_V8>"
    )
    return spm_text(stale=stale, volatile=volatile).replace(
        "<Generators>",
        "".join(blocks) + "<Generators>",
        1,
    ).replace("<Assets>", "<Assets>" + material, 1)


def write_spm(path, text):
    path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))


def legacy_receipt(snapshot, expected_mesh_ids, backup_name, *, schema_version=2):
    target = snapshot["target_projection"]
    receipt = {
        "kind": "speedtree_stale_node_table_preimage_receipt",
        "schema_version": schema_version,
        "recovery_contract": RECOVERY_CONTRACT,
        **snapshot["source_identity"],
        "exact_preimage": {
            "raw_sha256": snapshot["raw_sha256"],
            "spm_text_sha256": snapshot["text_sha256"],
            "size": snapshot["size"],
            "backup_file": backup_name,
            "backup_raw_sha256": snapshot["raw_sha256"],
        },
        "authoring_graph_projection": {
            "contract": "speedtree_spm_authoring_graph_projection",
            "version": 1,
            "fingerprint": snapshot["authoring_graph_fingerprint"],
        },
        "generator_membership": {
            "contract": "speedtree_generator_membership_projection",
            "version": 1,
            "count": snapshot["elementtree"]["generator_count"],
            "fingerprint": snapshot["generator_membership_fingerprint"],
        },
        "required_target_bindings": {
            "contract": "speedtree_required_target_binding_projection",
            "version": 1,
            "expected_mesh_ids": list(expected_mesh_ids),
            "binding_count": target["binding_count"],
            "fingerprint": _legacy_target_binding_fingerprint(
                snapshot["delivery"],
                expected_mesh_ids,
            ),
        },
    }
    if schema_version == 3:
        legacy_core = _legacy_authoring_graph_core_v2_projection(
            snapshot["text"]
        )
        receipt["authoring_graph_core_projection"] = {
            key: value
            for key, value in legacy_core.items()
            if not key.startswith("_")
        }
    return receipt


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ExitedProcess:
    pid = 1234

    def poll(self):
        return 0


def open_guards(state=None):
    if state is None:
        state = {}
    return {
        "is_cancelled": lambda: bool(state.get("cancelled")),
        "is_app_open": lambda: not bool(state.get("app_closed")),
        "is_job_current": lambda: not bool(state.get("job_stale")),
    }


class RecoveryTestCase(unittest.TestCase):
    def make_files(self, folder):
        spm = folder / "model.spm"
        executable = folder / "SpeedTree_Modeler.exe"
        write_spm(spm, spm_text(stale=True))
        executable.write_bytes(b"stub executable")
        recovery_root = folder / "recovery"
        return spm, executable, recovery_root

    def recover_with_save(
        self,
        spm,
        executable,
        recovery_root,
        *,
        after_text=None,
        retry=None,
        job_id=None,
        generation=None,
        guards=None,
        capture_fn=_capture_immutable_snapshot,
        launch_observer=None,
        timeout=10,
        expected_mesh_ids=TARGET_MESH_IDS,
        authoring_mesh_ids=None,
        required_live_mesh_ids=None,
    ):
        clock = FakeClock()
        after_text = after_text or spm_text(stale=False, volatile="two")

        def launch(exe, path):
            if launch_observer:
                launch_observer(exe, path)
            write_spm(spm, after_text)
            return ExitedProcess()

        return recover_stale_node_table(
            spm,
            executable,
            expected_mesh_ids,
            authoring_mesh_ids=authoring_mesh_ids,
            required_live_mesh_ids=required_live_mesh_ids,
            timeout=timeout,
            poll_interval=1,
            stable_reads=2,
            retry=retry,
            job_id=job_id,
            job_generation=generation,
            guards=guards,
            recovery_root=recovery_root,
            capture_fn=capture_fn,
            launch_fn=launch,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )


class OriginalFailureAndProjectionTests(RecoveryTestCase):
    def test_target_scope_resolver_is_explicit_and_fail_closed(self):
        strict, error = _resolve_target_scopes(TARGET_MESH_IDS)
        self.assertIsNone(error)
        self.assertEqual(strict["authoring_mesh_ids"], list(TARGET_MESH_IDS))
        self.assertEqual(strict["required_live_mesh_ids"], list(TARGET_MESH_IDS))

        subset, error = _resolve_target_scopes(
            (),
            authoring_mesh_ids=TARGET_MESH_IDS,
            required_live_mesh_ids=(130,),
        )
        self.assertIsNone(error)
        self.assertEqual(subset["required_live_mesh_ids"], [130])

        continuity, error = _resolve_target_scopes(
            (),
            authoring_mesh_ids=TARGET_MESH_IDS,
            required_live_mesh_ids=(),
        )
        self.assertIsNone(error)
        self.assertEqual(continuity["required_live_mesh_ids"], [])

        failures = (
            ((TARGET_MESH_IDS,), {"authoring_mesh_ids": TARGET_MESH_IDS,
                                 "required_live_mesh_ids": TARGET_MESH_IDS},
             "target_scope_mode_mixed"),
            (((),), {"authoring_mesh_ids": TARGET_MESH_IDS},
             "required_live_mesh_ids_missing"),
            (((),), {"authoring_mesh_ids": (), "required_live_mesh_ids": ()},
             "authoring_mesh_ids_missing"),
            (((),), {"authoring_mesh_ids": (130,),
                     "required_live_mesh_ids": (131,)},
             "required_live_scope_not_authoring_subset"),
            (((),), {"authoring_mesh_ids": (130,),
                     "required_live_mesh_ids": (0,)},
             "required_live_mesh_ids_invalid"),
        )
        for positional, keywords, token in failures:
            with self.subTest(token=token):
                _scopes, observed = _resolve_target_scopes(
                    *positional,
                    **keywords,
                )
                self.assertEqual(observed, token)

    def test_cli_exposes_legacy_subset_and_continuity_only_modes(self):
        parser = build_parser()
        legacy = parser.parse_args([
            "model.spm",
            "--expected-mesh-id", "130",
        ])
        self.assertEqual(legacy.expected_mesh_id, [130])
        self.assertIsNone(legacy.authoring_mesh_id)

        subset = parser.parse_args([
            "model.spm",
            "--authoring-mesh-id", "130",
            "--authoring-mesh-id", "131",
            "--required-live-mesh-id", "130",
        ])
        self.assertEqual(subset.authoring_mesh_id, [130, 131])
        self.assertEqual(subset.required_live_mesh_id, [130])

        continuity = parser.parse_args([
            "model.spm",
            "--authoring-mesh-id", "130",
            "--no-required-live-delivery",
        ])
        self.assertTrue(continuity.no_required_live_delivery)

        for argv in (
            ["model.spm", "--expected-mesh-id", "130",
             "--authoring-mesh-id", "130"],
            ["model.spm", "--authoring-mesh-id", "130",
             "--required-live-mesh-id", "130",
             "--no-required-live-delivery"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(argv)

    def test_authoring_core_accepts_only_observed_modeler_save_normalizations(self):
        baseline = spm_text(stale=True)
        before_property = (
            "<Property><Name>Custom:Density</Name><Value>1</Value></Property>"
        )
        before_extra = (
            "<Property><Name>Generation:Collections:old cutout</Name>"
            "<Value>false</Value></Property>"
            "<Property><Name>Random Seeds:Style</Name>"
            "<Value>919820633</Value></Property>"
            "<SplineProperty><Name>Physics:Bones</Name>"
            "<Value>0.4419</Value><CompoundParentSpline Count=\"1\">"
            "<Spline DrawMode=\"false\">"
            "<ControlPoint><X>0</X><Y>1</Y><TangentX>1</TangentX>"
            "<TangentY>0</TangentY><Length>0</Length></ControlPoint>"
            "<ControlPoint><X>1</X><Y>1</Y><TangentX>1</TangentX>"
            "<TangentY>0</TangentY><Length>0</Length></ControlPoint>"
            "</Spline></CompoundParentSpline>"
            "<ProfileSpline DrawMode=\"false\"><ControlPoint>"
            "<X>0</X><Y>1</Y><TangentX>0.24253584444522858</TangentX>"
            "<TangentY>-0.9701424241065979</TangentY><Length>0</Length>"
            "</ControlPoint></ProfileSpline></SplineProperty>"
        )
        after_extra = (
            "<Property><Name>Generation:Collections:new cutout</Name>"
            "<Value>false</Value></Property>"
            "<Property><Name>Random Seeds:Style</Name><Value>1</Value></Property>"
            "<SplineProperty><Name>Physics:Bones</Name>"
            "<Value>0.44190001487731934</Value>"
            "<CompoundParentSpline Count=\"0\" />"
            "<ProfileSpline DrawMode=\"false\"><ControlPoint>"
            "<X>0</X><Y>1</Y><TangentX>0.24253587424755096</TangentX>"
            "<TangentY>-0.97014254331588745</TangentY><Length>0</Length>"
            "</ControlPoint></ProfileSpline></SplineProperty>"
        )
        before = baseline.replace(
            before_property,
            before_property + before_extra,
            1,
        )
        after = spm_text(stale=False).replace(
            before_property,
            before_property + after_extra,
            1,
        )
        for marker in ("before", "after"):
            value = before if marker == "before" else after
            value = value.replace(
                "<Generators>",
                "<GlobalSettings><AuthoredValue>1</AuthoredValue>"
                "</GlobalSettings><Generators>",
                1,
            ).replace(
                "</Mesh>",
                "<VertexData><Value>1</Value></VertexData></Mesh>",
                1,
            )
            if marker == "before":
                before = value
            else:
                after = value

        self.assertNotEqual(
            spm_authoring_graph_fingerprint(before),
            spm_authoring_graph_fingerprint(after),
        )
        self.assertEqual(
            _authoring_graph_core_projection(before)["fingerprint"],
            _authoring_graph_core_projection(after)["fingerprint"],
        )
        changed = after.replace(
            "<Name>Custom:Density</Name><Value>1</Value>",
            "<Name>Custom:Density</Name><Value>2</Value>",
            1,
        )
        self.assertNotEqual(
            _authoring_graph_core_projection(before)["fingerprint"],
            _authoring_graph_core_projection(changed)["fingerprint"],
        )
        for changed in (
            after.replace(
                "<TangentX>0.24253587424755096</TangentX>",
                "<TangentX>0.25</TangentX>",
                1,
            ),
            after.replace(
                "<SourceGUID>root-guid</SourceGUID>",
                "<SourceGUID>other-root</SourceGUID>",
                1,
            ),
            after.replace(
                "<Name>mesh-130</Name>",
                "<Name>mesh-130-changed</Name>",
                1,
            ),
            after.replace(
                "<AuthoredValue>1</AuthoredValue>",
                "<AuthoredValue>2</AuthoredValue>",
                1,
            ),
            after.replace(
                "<VertexData><Value>1</Value></VertexData>",
                "<VertexData><Value>2</Value></VertexData>",
                1,
            ),
        ):
            self.assertNotEqual(
                _authoring_graph_core_projection(before)["fingerprint"],
                _authoring_graph_core_projection(changed)["fingerprint"],
            )

    def test_authoring_core_preserves_root_and_material_authored_values(self):
        before = authored_scope_text(
            stale=True,
            guid_suffix="before",
            volatile="before-cache",
        )
        no_edit_save = authored_scope_text(
            stale=False,
            guid_suffix="after",
            volatile="after-cache",
        )
        self.assertEqual(
            _authoring_graph_core_projection(before)["fingerprint"],
            _authoring_graph_core_projection(no_edit_save)["fingerprint"],
        )
        for tag in ("Force", "RuleScript", "Fan", "Light"):
            with self.subTest(tag=tag):
                changed = authored_scope_text(
                    stale=False,
                    guid_suffix="after",
                    volatile="after-cache",
                    root_values={tag: "2"},
                )
                self.assertNotEqual(
                    _authoring_graph_core_projection(before)["fingerprint"],
                    _authoring_graph_core_projection(changed)["fingerprint"],
                )
        changed_material = authored_scope_text(
            stale=False,
            guid_suffix="after",
            volatile="after-cache",
            material_filename="leaf_b.png",
        )
        self.assertNotEqual(
            _authoring_graph_core_projection(before)["fingerprint"],
            _authoring_graph_core_projection(changed_material)["fingerprint"],
        )

    def test_elementtree_audit_uses_canonical_generator_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = Path(temporary) / "model.spm"
            text = spm_text(stale=False)
            text = text.replace(
                "<GUID>g-130</GUID>",
                f"<GUID>{MINTED_GENERATOR_GUID}</GUID>",
            ).replace(
                "<TargetGUID>g-130</TargetGUID>",
                f"<TargetGUID>{MINTED_GENERATOR_GUID}</TargetGUID>",
            ).replace(
                "<GeneratorGUID>g-130</GeneratorGUID>",
                f"<GeneratorGUID>{MODELER_GENERATOR_GUID}</GeneratorGUID>",
            )
            write_spm(spm, text)

            snapshot = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)

        self.assertTrue(snapshot["regex_elementtree_parity"])
        self.assertEqual(
            snapshot["regex"]["eligible_owner_counts_fingerprint"],
            snapshot["elementtree"]["eligible_owner_counts_fingerprint"],
        )
        self.assertFalse(snapshot["delivery"]["node_table"]["stale"])

    def test_original_stale_blackgum_failure_shape_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, _root = self.make_files(folder)
            first = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            second = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)

            for snapshot in (first, second):
                self.assertTrue(snapshot["delivery"]["node_table"]["stale"])
                self.assertEqual(
                    snapshot["delivery"]["node_table"]["orphan_node_count"],
                    4,
                )
                self.assertTrue(snapshot["target_projection"]["complete"])
                self.assertTrue(snapshot["regex_elementtree_parity"])
                self.assertEqual(
                    snapshot["normalization"]["delivery_reason"],
                    "live_export_evidence_unavailable_stale_node_table",
                )
                self.assertFalse(snapshot["normalization"]["complete"])
            self.assertEqual(first["raw_sha256"], second["raw_sha256"])
            self.assertEqual(
                first["authoring_graph_fingerprint"],
                second["authoring_graph_fingerprint"],
            )

    def test_authoring_projection_ignores_only_nodes_and_known_volatile_data(self):
        baseline = spm_text(stale=True, volatile="one")
        resaved = spm_text(stale=False, volatile="two")
        self.assertEqual(
            spm_authoring_graph_fingerprint(baseline),
            spm_authoring_graph_fingerprint(resaved),
        )
        for changed in (
            spm_text(stale=False, graph_property="2"),
            spm_text(stale=False, link_source="another-root"),
            spm_text(stale=False, mesh_name_suffix="-changed"),
        ):
            self.assertNotEqual(
                spm_authoring_graph_fingerprint(baseline),
                spm_authoring_graph_fingerprint(changed),
            )
        nested_nodes_a = baseline.replace(
            "</Assets>",
            "<StructuralGraph><Nodes><Point>A</Point></Nodes></StructuralGraph></Assets>",
        )
        nested_nodes_b = baseline.replace(
            "</Assets>",
            "<StructuralGraph><Nodes><Point>B</Point></Nodes></StructuralGraph></Assets>",
        )
        self.assertNotEqual(
            spm_authoring_graph_fingerprint(nested_nodes_a),
            spm_authoring_graph_fingerprint(nested_nodes_b),
        )

    def test_contract_publishes_every_forbidden_and_required_boundary(self):
        contract = _stale_node_table_recovery_contract()
        self.assertEqual(contract["schema_version"], 2)
        self.assertFalse(contract["modeler_auto_save"])
        self.assertFalse(contract["modeler_process_kill"])
        self.assertFalse(contract["direct_spm_xml_edit"])
        self.assertFalse(contract["ui_input_simulation"])
        self.assertFalse(contract["automatic_rollback"])
        self.assertFalse(contract["stale_false_alone_allows_retry"])
        self.assertTrue(contract["requires_exact_preimage_backup"])
        self.assertTrue(contract["requires_immutable_preimage_receipt"])
        self.assertTrue(contract["source_sha_rechecked_before_continuation"])
        self.assertTrue(contract["continuation_once_only"])
        self.assertFalse(contract["queue_or_manifest_mutation_before_continuation"])


class PreimageAndReceiptTests(RecoveryTestCase):
    def test_sealed_reaudit_rejects_root_and_material_authored_changes(self):
        mutations = (
            ("Force", None),
            ("RuleScript", None),
            ("Fan", None),
            ("Light", None),
            (None, "leaf_b.png"),
        )
        for root_tag, material_filename in mutations:
            label = root_tag or "Material_V8"
            with self.subTest(scope=label), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, _executable, root = self.make_files(folder)
                root.mkdir()
                write_spm(spm, authored_scope_text(
                    stale=True,
                    guid_suffix="before",
                    volatile="before-cache",
                ))
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline,
                    TARGET_MESH_IDS,
                    root,
                )
                write_spm(spm, authored_scope_text(
                    stale=False,
                    guid_suffix="after",
                    volatile="after-cache",
                    root_values={root_tag: "2"} if root_tag else None,
                    material_filename=material_filename or "leaf_a.png",
                ))

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    verify_sealed_resave(
                        spm,
                        artifacts["backup_path"],
                        artifacts["receipt_path"],
                        TARGET_MESH_IDS,
                    )

            self.assertEqual(
                caught.exception.reason_token,
                "sealed_resave_reaudit_failed",
            )
            self.assertIn(
                "authoring_graph_changed_during_resave",
                caught.exception.evidence["reason_tokens"],
            )

    def test_recovery_validates_every_target_material_scope(self):
        material_by_mesh = {
            130: 10,
            131: 10,
            132: 11,
            133: 11,
        }
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            write_spm(
                spm,
                spm_text(stale=True, material_by_mesh=material_by_mesh),
            )

            result = self.recover_with_save(
                spm,
                executable,
                root,
                after_text=spm_text(
                    stale=False,
                    volatile="two",
                    material_by_mesh=material_by_mesh,
                ),
            )

        normalization = result["reaudit"]["normalization"]
        self.assertTrue(normalization["complete"])
        self.assertEqual(normalization["material_scope_count"], 2)
        self.assertTrue(all(
            scope["complete"] for scope in normalization["material_scopes"]
        ))

    def test_explicit_required_live_subset_preserves_all_authoring_bindings(self):
        after = spm_text(stale=False, volatile="two")
        for mesh_id in (131, 132, 133):
            after = after.replace(_node(f"g-{mesh_id}"), "", 1)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            result = self.recover_with_save(
                spm,
                executable,
                root,
                after_text=after,
                expected_mesh_ids=(),
                authoring_mesh_ids=TARGET_MESH_IDS,
                required_live_mesh_ids=(130,),
            )
            receipt = json.loads(
                next(root.glob("*.receipt.json")).read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "repaired_reaudit_valid")
        self.assertEqual(
            result["reaudit"]["sealed_authoring_mesh_ids"],
            list(TARGET_MESH_IDS),
        )
        self.assertEqual(
            result["reaudit"]["sealed_required_delivery_mesh_ids"],
            [130],
        )
        self.assertTrue(result["reaudit"]["normalization"]["applicable"])
        self.assertEqual(receipt["schema_version"], 6)
        self.assertEqual(
            receipt["target_requirements"]["required_live_mesh_ids"],
            [130],
        )

    def test_explicit_authoring_scope_preserves_hidden_non_live_binding(self):
        visible = "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>false</Hidden>"
        hidden = "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>true</Hidden>"
        before = spm_text(stale=True).replace(visible, hidden, 1)
        after = spm_text(stale=False, volatile="two").replace(visible, hidden, 1)
        for mesh_id in (131, 132):
            after = after.replace(_node(f"g-{mesh_id}"), "", 1)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            write_spm(spm, before)
            result = self.recover_with_save(
                spm,
                executable,
                root,
                after_text=after,
                expected_mesh_ids=(),
                authoring_mesh_ids=TARGET_MESH_IDS,
                required_live_mesh_ids=(130,),
            )

        self.assertEqual(result["status"], "repaired_reaudit_valid")
        self.assertTrue(result["reaudit"]["required_target_binding_continuity"])

    def test_explicit_binding_continuity_only_accepts_zero_node_bindings(self):
        after = spm_text(stale=False, volatile="two")
        for mesh_id in TARGET_MESH_IDS:
            after = after.replace(_node(f"g-{mesh_id}"), "", 1)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            result = self.recover_with_save(
                spm,
                executable,
                root,
                after_text=after,
                expected_mesh_ids=(),
                authoring_mesh_ids=TARGET_MESH_IDS,
                required_live_mesh_ids=(),
            )

        self.assertEqual(result["status"], "repaired_reaudit_valid")
        normalization = result["reaudit"]["normalization"]
        self.assertFalse(normalization["applicable"])
        self.assertEqual(normalization["status"], "not_required")
        self.assertNotIn("complete", normalization)
        self.assertEqual(
            result["reaudit"]["target_delivery"]["required_live_binding_count"],
            0,
        )

    def test_binding_continuity_only_still_rejects_common_integrity_changes(self):
        generator_133 = (
            '<Generator Type="Frond"><Name>Leaf 133</Name><GUID>g-133</GUID>'
            '<Hidden>false</Hidden><Properties><Property><Name>Leaf:Material</Name>'
            '<Value>10</Value></Property><Property><Name>Leaf:Mesh</Name>'
            '<Value>133</Value></Property><Property><Name>Custom:Density</Name>'
            '<Value>1</Value></Property></Properties></Generator>'
        )
        cases = {
            "stale_orphan": spm_text(stale=True, volatile="two"),
            "core": spm_text(stale=False, volatile="two", graph_property="2"),
            "membership": spm_text(stale=False, volatile="two").replace(
                generator_133, "", 1
            ),
            "binding": spm_text(stale=False, volatile="two").replace(
                "<Value>133</Value></Property>",
                "<Value>132</Value></Property>",
                1,
            ),
        }
        expected_tokens = {
            "stale_orphan": "node_table_still_stale",
            "core": "authoring_graph_changed_during_resave",
            "membership": "generator_membership_changed_during_resave",
            "binding": "required_target_bindings_changed_during_resave",
        }
        for label, after in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                    self.recover_with_save(
                        spm,
                        executable,
                        root,
                        after_text=after,
                        expected_mesh_ids=(),
                        authoring_mesh_ids=TARGET_MESH_IDS,
                        required_live_mesh_ids=(),
                        timeout=3,
                    )
            self.assertIn(
                expected_tokens[label],
                caught.exception.evidence["last_reason_tokens"],
            )
            if label == "binding":
                self.assertIn(
                    "required_target_manifest_incomplete_after_resave",
                    caught.exception.evidence["last_reason_tokens"],
                )

    def test_schema6_scope_tamper_and_caller_mismatch_fail_closed(self):
        for mutation in ("policy", "outside_subset", "version", "caller"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, _executable, root = self.make_files(folder)
                root.mkdir()
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline,
                    (),
                    root,
                    authoring_mesh_ids=TARGET_MESH_IDS,
                    required_live_mesh_ids=(130,),
                )
                receipt = artifacts["receipt"]
                if mutation == "policy":
                    receipt["target_requirements"]["policy"] = "post_save_auto"
                elif mutation == "outside_subset":
                    receipt["target_requirements"]["required_live_mesh_ids"] = [999]
                elif mutation == "version":
                    receipt["target_requirements"]["version"] = 99
                artifacts["receipt_path"].write_text(
                    json.dumps(receipt, sort_keys=True),
                    encoding="utf-8",
                )
                write_spm(spm, spm_text(stale=False, volatile="two"))

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    verify_sealed_resave(
                        spm,
                        artifacts["backup_path"],
                        artifacts["receipt_path"],
                        (),
                        authoring_mesh_ids=TARGET_MESH_IDS,
                        required_live_mesh_ids=(
                            () if mutation == "caller" else (130,)
                        ),
                    )

            expected_token = (
                "preimage_receipt_projection_version_unsupported"
                if mutation == "version"
                else "preimage_receipt_verification_failed"
            )
            self.assertEqual(caught.exception.reason_token, expected_token)

    def test_schema2_through_4_receipts_remain_strict_only(self):
        for schema_version in (2, 3, 4):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, _executable, root = self.make_files(folder)
                root.mkdir()
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline,
                    TARGET_MESH_IDS,
                    root,
                )
                if schema_version in (2, 3):
                    receipt = legacy_receipt(
                        baseline,
                        TARGET_MESH_IDS,
                        artifacts["backup_path"].name,
                        schema_version=schema_version,
                    )
                else:
                    receipt = dict(artifacts["receipt"])
                    receipt["schema_version"] = 4
                    receipt["authoring_graph_core_projection"] = {
                        key: value
                        for key, value in (
                            _legacy_authoring_graph_core_v3_projection(
                                baseline["text"]
                            )
                        ).items()
                        if not key.startswith("_")
                    }
                    receipt.pop("target_requirements")
                artifacts["receipt_path"].write_text(
                    json.dumps(receipt, sort_keys=True),
                    encoding="utf-8",
                )
                write_spm(spm, spm_text(stale=False, volatile="two"))

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    verify_sealed_resave(
                        spm,
                        artifacts["backup_path"],
                        artifacts["receipt_path"],
                        (),
                        authoring_mesh_ids=TARGET_MESH_IDS,
                        required_live_mesh_ids=(),
                    )

            self.assertEqual(
                caught.exception.reason_token,
                "preimage_receipt_verification_failed",
            )

    def test_schema6_restart_reuses_sealed_scope_receipt(self):
        after = spm_text(stale=False, volatile="two")
        for mesh_id in (131, 132, 133):
            after = after.replace(_node(f"g-{mesh_id}"), "", 1)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            root.mkdir()
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline,
                (),
                root,
                authoring_mesh_ids=TARGET_MESH_IDS,
                required_live_mesh_ids=(130,),
            )
            receipt_bytes = artifacts["receipt_path"].read_bytes()

            result = self.recover_with_save(
                spm,
                executable,
                root,
                after_text=after,
                expected_mesh_ids=(),
                authoring_mesh_ids=TARGET_MESH_IDS,
                required_live_mesh_ids=(130,),
            )

            self.assertEqual(
                artifacts["receipt_path"].read_bytes(),
                receipt_bytes,
            )
        self.assertEqual(result["status"], "repaired_reaudit_valid")

    def test_schema5_core_v3_receipt_reaudits_byte_for_byte_under_v4(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline,
                (),
                root,
                authoring_mesh_ids=TARGET_MESH_IDS,
                required_live_mesh_ids=TARGET_MESH_IDS,
            )
            receipt = json.loads(json.dumps(artifacts["receipt"]))
            receipt["schema_version"] = 5
            receipt["authoring_graph_core_projection"] = {
                key: value
                for key, value in (
                    _legacy_authoring_graph_core_v3_projection(
                        baseline["text"]
                    )
                ).items()
                if not key.startswith("_")
            }
            artifacts["receipt_path"].write_text(
                json.dumps(receipt, sort_keys=True),
                encoding="utf-8",
            )
            sealed_receipt_bytes = artifacts["receipt_path"].read_bytes()
            write_spm(spm, spm_text(stale=False, volatile="two"))

            result = verify_sealed_resave(
                spm,
                artifacts["backup_path"],
                artifacts["receipt_path"],
                (),
                authoring_mesh_ids=TARGET_MESH_IDS,
                required_live_mesh_ids=TARGET_MESH_IDS,
            )

            self.assertEqual(
                artifacts["receipt_path"].read_bytes(),
                sealed_receipt_bytes,
            )
        self.assertEqual(result["status"], "sealed_resave_reaudit_valid")
        self.assertEqual(
            result["reaudit"]["authoring_graph_core_projection_version"],
            4,
        )

    def test_exact_backup_and_immutable_receipt_exist_before_modeler_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            preimage = spm.read_bytes()

            def assert_sealed(_exe, _path):
                backups = list(root.glob("*.preimage.spm"))
                receipts = list(root.glob("*.receipt.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(len(receipts), 1)
                self.assertEqual(backups[0].read_bytes(), preimage)
                receipt_text = receipts[0].read_text(encoding="utf-8")
                receipt = json.loads(receipt_text)
                self.assertEqual(receipt["schema_version"], 6)
                self.assertEqual(
                    receipt["authoring_graph_projection"]["version"], 1
                )
                self.assertEqual(
                    receipt["authoring_graph_core_projection"]["version"], 4
                )
                self.assertEqual(receipt["generator_membership"]["version"], 1)
                self.assertEqual(receipt["required_target_bindings"]["version"], 2)
                self.assertEqual(
                    receipt["target_requirements"]["policy"],
                    "explicit_sealed_scopes_v1",
                )
                self.assertEqual(
                    receipt["target_requirements"]["authoring_mesh_ids"],
                    list(TARGET_MESH_IDS),
                )
                self.assertEqual(
                    receipt["target_requirements"]["required_live_mesh_ids"],
                    list(TARGET_MESH_IDS),
                )
                self.assertNotIn(str(folder), receipt_text)
                self.assertNotIn("g-130", receipt_text)

            result = self.recover_with_save(
                spm,
                executable,
                root,
                launch_observer=assert_sealed,
            )
            self.assertEqual(result["contract"], RECOVERY_CONTRACT)
            self.assertEqual(result["status"], "repaired_reaudit_valid")
            self.assertTrue(result["reaudit"]["authoring_graph_continuity"])
            self.assertTrue(result["reaudit"]["regex_elementtree_parity"])
            self.assertTrue(result["reaudit"]["normalization"]["complete"])
            self.assertEqual(
                result["reaudit"]["normalization"]["live_snapshot_sha256"],
                result["after_sha256"],
            )

    def test_real_v2_receipt_with_modeler_guid_dialect_reaudits_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            preimage_text = spm_text(stale=True).replace(
                "<GUID>g-130</GUID>",
                f"<GUID>{MODELER_GENERATOR_GUID}</GUID>",
            ).replace(
                "<TargetGUID>g-130</TargetGUID>",
                f"<TargetGUID>{MODELER_GENERATOR_GUID}</TargetGUID>",
            ).replace(
                "<GeneratorGUID>orphan-guid</GeneratorGUID>",
                f"<GeneratorGUID>{MINTED_GENERATOR_GUID}</GeneratorGUID>",
                1,
            )
            write_spm(spm, preimage_text)
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline,
                TARGET_MESH_IDS,
                root,
            )
            legacy = legacy_receipt(
                baseline,
                TARGET_MESH_IDS,
                artifacts["backup_path"].name,
                schema_version=2,
            )
            artifacts["receipt_path"].write_text(
                json.dumps(legacy, sort_keys=True),
                encoding="utf-8",
            )
            sealed_receipt_bytes = artifacts["receipt_path"].read_bytes()
            after_text = spm_text(stale=False, volatile="two").replace(
                "<GUID>g-130</GUID>",
                f"<GUID>{MODELER_GENERATOR_GUID}</GUID>",
            ).replace(
                "<TargetGUID>g-130</TargetGUID>",
                f"<TargetGUID>{MODELER_GENERATOR_GUID}</TargetGUID>",
            ).replace(
                "<GeneratorGUID>g-130</GeneratorGUID>",
                f"<GeneratorGUID>{MINTED_GENERATOR_GUID}</GeneratorGUID>",
            )
            write_spm(spm, after_text)

            result = verify_sealed_resave(
                spm,
                artifacts["backup_path"],
                artifacts["receipt_path"],
                TARGET_MESH_IDS,
            )
            self.assertEqual(
                artifacts["receipt_path"].read_bytes(),
                sealed_receipt_bytes,
            )

        self.assertEqual(result["status"], "sealed_resave_reaudit_valid")
        self.assertFalse(result["modeler_launched"])
        self.assertTrue(result["reaudit"]["authoring_graph_continuity"])

    def test_ensure_reuses_exact_valid_v2_receipt_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline,
                TARGET_MESH_IDS,
                root,
            )
            legacy = legacy_receipt(
                baseline,
                TARGET_MESH_IDS,
                artifacts["backup_path"].name,
                schema_version=2,
            )
            artifacts["receipt_path"].write_text(
                json.dumps(legacy, sort_keys=True),
                encoding="utf-8",
            )
            sealed_bytes = artifacts["receipt_path"].read_bytes()
            sealed_sha = hashlib.sha256(sealed_bytes).hexdigest()

            reused = _ensure_preimage_artifacts(
                baseline,
                TARGET_MESH_IDS,
                root,
            )

            self.assertEqual(reused["receipt"], legacy)
            self.assertEqual(reused["receipt_path"].read_bytes(), sealed_bytes)
            self.assertEqual(reused["receipt_sha256"], sealed_sha)

    def test_schema3_core_v2_is_rebuilt_before_current_v4_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline,
                TARGET_MESH_IDS,
                root,
            )
            receipt = legacy_receipt(
                baseline,
                TARGET_MESH_IDS,
                artifacts["backup_path"].name,
                schema_version=3,
            )
            artifacts["receipt_path"].write_text(
                json.dumps(receipt, sort_keys=True),
                encoding="utf-8",
            )
            write_spm(spm, spm_text(stale=False, volatile="two"))

            result = verify_sealed_resave(
                spm,
                artifacts["backup_path"],
                artifacts["receipt_path"],
                TARGET_MESH_IDS,
            )

        self.assertEqual(result["status"], "sealed_resave_reaudit_valid")
        self.assertFalse(result["modeler_launched"])
        self.assertEqual(
            result["reaudit"]["authoring_graph_core_projection_version"],
            4,
        )

    def test_receipt_schema_versions_require_exact_integer_types(self):
        for schema_version in (True, "5", 5.0, 99):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, _executable, root = self.make_files(folder)
                root.mkdir()
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline,
                    TARGET_MESH_IDS,
                    root,
                )
                receipt = json.loads(json.dumps(artifacts["receipt"]))
                receipt["schema_version"] = schema_version
                artifacts["receipt_path"].write_text(
                    json.dumps(receipt, sort_keys=True),
                    encoding="utf-8",
                )
                sealed_bytes = artifacts["receipt_path"].read_bytes()
                write_spm(spm, spm_text(stale=False, volatile="two"))

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    verify_sealed_resave(
                        spm,
                        artifacts["backup_path"],
                        artifacts["receipt_path"],
                        TARGET_MESH_IDS,
                    )

                self.assertEqual(
                    caught.exception.reason_token,
                    "preimage_receipt_schema_unsupported",
                )
                self.assertEqual(
                    artifacts["receipt_path"].read_bytes(),
                    sealed_bytes,
                )

    def test_known_schema3_core_v1_is_unsupported_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline,
                TARGET_MESH_IDS,
                root,
            )
            receipt = legacy_receipt(
                baseline,
                TARGET_MESH_IDS,
                artifacts["backup_path"].name,
                schema_version=3,
            )
            receipt["authoring_graph_core_projection"]["version"] = 1
            artifacts["receipt_path"].write_text(
                json.dumps(receipt, sort_keys=True),
                encoding="utf-8",
            )
            sealed_bytes = artifacts["receipt_path"].read_bytes()
            write_spm(spm, spm_text(stale=False, volatile="two"))

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                verify_sealed_resave(
                    spm,
                    artifacts["backup_path"],
                    artifacts["receipt_path"],
                    TARGET_MESH_IDS,
                )

            self.assertEqual(
                caught.exception.reason_token,
                "preimage_receipt_projection_version_unsupported",
            )
            self.assertEqual(
                artifacts["receipt_path"].read_bytes(),
                sealed_bytes,
            )
            write_spm(
                artifacts["backup_path"],
                spm_text(stale=True, graph_property="tampered"),
            )
            with self.assertRaises(StaleNodeTableRecoveryError) as tampered:
                verify_sealed_resave(
                    spm,
                    artifacts["backup_path"],
                    artifacts["receipt_path"],
                    TARGET_MESH_IDS,
                )
            self.assertEqual(
                tampered.exception.reason_token,
                "preimage_backup_verification_failed",
            )

    def test_schema6_authoritative_projection_fields_are_verified(self):
        mutations = (
            ("membership_count", "generator_membership", "count", 99),
            ("binding_count", "required_target_bindings", "binding_count", 99),
            (
                "expected_mesh_ids",
                "required_target_bindings",
                "expected_mesh_ids",
                [130, 131, 132],
            ),
            (
                "core_generator_count",
                "authoring_graph_core_projection",
                "generator_count",
                99,
            ),
        )
        for label, block, field, value in mutations:
            with self.subTest(mutation=label), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, _executable, root = self.make_files(folder)
                root.mkdir()
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline,
                    TARGET_MESH_IDS,
                    root,
                )
                receipt = json.loads(json.dumps(artifacts["receipt"]))
                receipt[block][field] = value
                artifacts["receipt_path"].write_text(
                    json.dumps(receipt, sort_keys=True),
                    encoding="utf-8",
                )
                write_spm(spm, spm_text(stale=False, volatile="two"))

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    verify_sealed_resave(
                        spm,
                        artifacts["backup_path"],
                        artifacts["receipt_path"],
                        TARGET_MESH_IDS,
                    )

                self.assertEqual(
                    caught.exception.reason_token,
                    "preimage_receipt_verification_failed",
                )

    def test_schema2_restart_keeps_preimage_context_for_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            root.mkdir()
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline,
                TARGET_MESH_IDS,
                root,
            )
            receipt = legacy_receipt(
                baseline,
                TARGET_MESH_IDS,
                artifacts["backup_path"].name,
                schema_version=2,
            )
            artifacts["receipt_path"].write_text(
                json.dumps(receipt, sort_keys=True),
                encoding="utf-8",
            )
            sealed_bytes = artifacts["receipt_path"].read_bytes()
            continuations = []

            result = self.recover_with_save(
                spm,
                executable,
                root,
                retry=lambda continuation: continuations.append(continuation),
                job_id="legacy-schema2",
                generation=1,
                guards=open_guards(),
            )

            self.assertEqual(
                result["status"],
                "repaired_reaudited_and_retried_once",
            )
            self.assertEqual(len(continuations), 1)
            self.assertEqual(
                artifacts["receipt_path"].read_bytes(),
                sealed_bytes,
            )

    def test_dialect_registry_is_independent_of_mutable_current_constants(self):
        receipt = {
            "schema_version": 4,
            "authoring_graph_projection": {
                "contract": "speedtree_spm_authoring_graph_projection",
                "version": 1,
            },
            "authoring_graph_core_projection": {
                "contract": "speedtree_spm_authoring_graph_core_projection",
                "version": 3,
            },
            "generator_membership": {
                "contract": "speedtree_generator_membership_projection",
                "version": 1,
            },
            "required_target_bindings": {
                "contract": "speedtree_required_target_binding_projection",
                "version": 2,
            },
        }
        self.assertEqual(
            _resolve_receipt_dialect(receipt),
            "schema4_graph1_core3_target2",
        )

    def test_legacy_receipt_tamper_and_unknown_version_fail_closed(self):
        for mutation in ("fingerprint", "version", "core_version"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, _executable, root = self.make_files(folder)
                root.mkdir()
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline,
                    TARGET_MESH_IDS,
                    root,
                )
                receipt = legacy_receipt(
                    baseline,
                    TARGET_MESH_IDS,
                    artifacts["backup_path"].name,
                    schema_version=2,
                )
                if mutation == "fingerprint":
                    receipt["required_target_bindings"]["fingerprint"] = "f" * 64
                elif mutation == "version":
                    receipt["required_target_bindings"]["version"] = 99
                else:
                    receipt = artifacts["receipt"]
                    receipt["authoring_graph_core_projection"]["version"] = 99
                artifacts["receipt_path"].write_text(
                    json.dumps(receipt, sort_keys=True),
                    encoding="utf-8",
                )
                write_spm(spm, spm_text(stale=False, volatile="two"))

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    verify_sealed_resave(
                        spm,
                        artifacts["backup_path"],
                        artifacts["receipt_path"],
                        TARGET_MESH_IDS,
                    )

            expected_token = (
                "preimage_receipt_projection_version_unsupported"
                if mutation in {"version", "core_version"}
                else "preimage_receipt_verification_failed"
            )
            self.assertEqual(caught.exception.reason_token, expected_token)

    def test_schema3_unknown_or_fake_known_core_fingerprint_is_rejected(self):
        for core_version in (99, 2):
            with self.subTest(core_version=core_version), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, _executable, root = self.make_files(folder)
                root.mkdir()
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline,
                    TARGET_MESH_IDS,
                    root,
                )
                receipt = legacy_receipt(
                    baseline,
                    TARGET_MESH_IDS,
                    artifacts["backup_path"].name,
                    schema_version=3,
                )
                receipt["authoring_graph_core_projection"]["version"] = core_version
                receipt["authoring_graph_core_projection"]["fingerprint"] = "f" * 64
                artifacts["receipt_path"].write_text(
                    json.dumps(receipt, sort_keys=True),
                    encoding="utf-8",
                )
                write_spm(spm, spm_text(stale=False, volatile="two"))

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    verify_sealed_resave(
                        spm,
                        artifacts["backup_path"],
                        artifacts["receipt_path"],
                        TARGET_MESH_IDS,
                    )

            expected_token = (
                "preimage_receipt_projection_version_unsupported"
                if core_version == 99
                else "preimage_receipt_verification_failed"
            )
            self.assertEqual(caught.exception.reason_token, expected_token)

    def test_legacy_hidden_requested_binding_remains_fail_closed(self):
        hidden = (
            "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>false</Hidden>"
        )
        hidden_replacement = (
            "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>true</Hidden>"
        )
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            write_spm(spm, spm_text(stale=True).replace(
                hidden,
                hidden_replacement,
                1,
            ))
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline,
                TARGET_MESH_IDS,
                root,
            )
            receipt = legacy_receipt(
                baseline,
                TARGET_MESH_IDS,
                artifacts["backup_path"].name,
                schema_version=2,
            )
            artifacts["receipt_path"].write_text(
                json.dumps(receipt, sort_keys=True),
                encoding="utf-8",
            )
            write_spm(spm, spm_text(stale=False, volatile="two").replace(
                hidden,
                hidden_replacement,
                1,
            ))

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                verify_sealed_resave(
                    spm,
                    artifacts["backup_path"],
                    artifacts["receipt_path"],
                    TARGET_MESH_IDS,
                )

        self.assertEqual(caught.exception.reason_token, "preimage_reaudit_failed")

    def test_missing_requested_mesh_id_blocks_before_modeler_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            snapshot = _capture_immutable_snapshot(
                spm,
                (*TARGET_MESH_IDS, 999),
            )
            self.assertFalse(snapshot["target_projection"]["complete"])
            self.assertEqual(
                snapshot["target_projection"]["missing_requested_mesh_ids"],
                [999],
            )
            launched = []
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    expected_mesh_ids=(*TARGET_MESH_IDS, 999),
                    launch_observer=lambda *_args: launched.append(True),
                )

        self.assertEqual(
            caught.exception.reason_token,
            "preimage_target_manifest_incomplete",
        )
        self.assertEqual(launched, [])

    def test_hidden_only_requested_mesh_blocks_before_modeler_launch(self):
        visible = "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>false</Hidden>"
        hidden = "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>true</Hidden>"
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            write_spm(spm, spm_text(stale=True).replace(visible, hidden, 1))
            snapshot = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            self.assertFalse(snapshot["target_projection"]["complete"])
            self.assertEqual(
                snapshot["target_projection"]["missing_requested_mesh_ids"],
                [133],
            )
            launched = []
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    launch_observer=lambda *_args: launched.append(True),
                )

        self.assertEqual(
            caught.exception.reason_token,
            "preimage_target_manifest_incomplete",
        )
        self.assertEqual(launched, [])

    def test_zero_node_requested_slots_remain_fail_closed_after_save(self):
        after = spm_text(stale=False, volatile="two")
        for mesh_id in (131, 132, 133):
            after = after.replace(_node(f"g-{mesh_id}"), "", 1)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)

            with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    after_text=after,
                    timeout=3,
                )

        self.assertIn(
            "target_binding_has_no_eligible_nodes",
            caught.exception.evidence["last_reason_tokens"],
        )
        self.assertIn(
            "target_binding_not_export_participating",
            caught.exception.evidence["last_reason_tokens"],
        )

    def test_same_mesh_one_live_three_dead_siblings_remain_fail_closed(self):
        baseline = spm_text(stale=True)
        after = spm_text(stale=False, volatile="two")
        for mesh_id in (131, 132, 133):
            old = (
                "<Property><Name>Leaf:Mesh</Name>"
                f"<Value>{mesh_id}</Value></Property>"
            )
            new = (
                "<Property><Name>Leaf:Mesh</Name>"
                "<Value>130</Value></Property>"
            )
            baseline = baseline.replace(old, new, 1)
            after = after.replace(old, new, 1)
            after = after.replace(_node(f"g-{mesh_id}"), "", 1)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            write_spm(spm, baseline)

            with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    after_text=after,
                    timeout=3,
                    expected_mesh_ids=(130,),
                )
            receipt = json.loads(
                next(root.glob("*.receipt.json")).read_text(encoding="utf-8")
            )

        self.assertEqual(
            receipt["required_target_bindings"]["expected_mesh_ids"],
            [130],
        )
        self.assertEqual(
            receipt["required_target_bindings"]["binding_count"],
            4,
        )
        self.assertIn(
            "target_binding_has_no_eligible_nodes",
            caught.exception.evidence["last_reason_tokens"],
        )
        self.assertIn(
            "target_binding_not_export_participating",
            caught.exception.evidence["last_reason_tokens"],
        )

    def test_corrupt_backup_or_receipt_blocks_before_launch(self):
        for corrupt in ("backup", "receipt"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                root.mkdir()
                baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
                artifacts = _ensure_preimage_artifacts(
                    baseline, TARGET_MESH_IDS, root
                )
                if corrupt == "backup":
                    artifacts["backup_path"].write_bytes(b"corrupt")
                    token = "preimage_backup_verification_failed"
                else:
                    artifacts["receipt_path"].write_text("{}", encoding="utf-8")
                    token = "preimage_receipt_verification_failed"
                launched = []
                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    recover_stale_node_table(
                        spm,
                        executable,
                        TARGET_MESH_IDS,
                        recovery_root=root,
                        launch_fn=lambda *_args: launched.append(True),
                    )
                self.assertEqual(caught.exception.reason_token, token)
                self.assertEqual(launched, [])

    def test_interrupted_backup_only_restart_rebuilds_receipt_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            root.mkdir()
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(baseline, TARGET_MESH_IDS, root)
            backup_bytes = artifacts["backup_path"].read_bytes()
            artifacts["receipt_path"].unlink()

            result = self.recover_with_save(spm, executable, root)
            self.assertEqual(result["status"], "repaired_reaudit_valid")
            self.assertEqual(artifacts["backup_path"].read_bytes(), backup_bytes)
            self.assertTrue(artifacts["receipt_path"].is_file())

    def test_source_change_between_seal_and_launch_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            calls = 0

            def racing_capture(path, expected):
                nonlocal calls
                calls += 1
                captured = _capture_immutable_snapshot(path, expected)
                if calls == 1:
                    write_spm(spm, spm_text(stale=True, graph_property="2"))
                return captured

            launched = []
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                recover_stale_node_table(
                    spm,
                    executable,
                    TARGET_MESH_IDS,
                    recovery_root=root,
                    capture_fn=racing_capture,
                    launch_fn=lambda *_args: launched.append(True),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "source_changed_before_modeler_launch",
            )
            self.assertEqual(launched, [])


class QuiescenceAndGraphGateTests(RecoveryTestCase):
    def test_graph_change_and_stale_false_alone_never_continue(self):
        no_live_target = spm_text(stale=False)
        for mesh_id in TARGET_MESH_IDS:
            no_live_target = no_live_target.replace(_node(f"g-{mesh_id}"), "", 1)
        no_live_target = no_live_target.replace(
            "<Nodes>",
            "<Nodes>" + _node("root-guid"),
            1,
        )
        for after_text, expected_reason in (
            (
                spm_text(stale=False, graph_property="2"),
                "authoring_graph_changed_during_resave",
            ),
            (
                no_live_target,
                "live_target_mesh_set_incomplete",
            ),
        ):
            with self.subTest(reason=expected_reason), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                    self.recover_with_save(
                        spm,
                        executable,
                        root,
                        after_text=after_text,
                        timeout=3,
                    )
                self.assertEqual(
                    caught.exception.reason_token,
                    "valid_resave_quiescence_timeout",
                )
                self.assertIn(
                    expected_reason,
                    caught.exception.evidence["last_reason_tokens"],
                )
                events = list(root.glob("blocked.*.json"))
                self.assertEqual(len(events), 1)
                event_text = events[0].read_text(encoding="utf-8")
                event = json.loads(event_text)
                self.assertEqual(event["asset_name"], "model.spm")
                self.assertRegex(event["after_sha256"], r"^[0-9a-f]{64}$")
                self.assertNotIn(str(folder), event_text)
                self.assertNotIn("g-130", event_text)

    def test_generator_membership_change_remains_fail_closed(self):
        changed = spm_text(stale=False).replace("g-133", "g-replaced")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    after_text=changed,
                    timeout=3,
                )

        self.assertIn(
            "generator_membership_changed_during_resave",
            caught.exception.evidence["last_reason_tokens"],
        )

    def test_transient_changed_snapshot_must_be_replaced_by_stable_valid_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            capture_calls = 0

            def evolving_capture(path, expected):
                nonlocal capture_calls
                capture_calls += 1
                captured = _capture_immutable_snapshot(path, expected)
                if capture_calls == 3:
                    write_spm(spm, spm_text(stale=False, volatile="final"))
                return captured

            result = self.recover_with_save(
                spm,
                executable,
                root,
                after_text=spm_text(stale=False, graph_property="2"),
                capture_fn=evolving_capture,
            )
            self.assertEqual(result["status"], "repaired_reaudit_valid")
            self.assertTrue(result["reaudit"]["valid"])

    def test_process_exit_never_substitutes_for_a_saved_quiescent_file(self):
        for iteration in range(10):
            with self.subTest(iteration=iteration), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                clock = FakeClock()
                retried = []
                with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                    recover_stale_node_table(
                        spm,
                        executable,
                        TARGET_MESH_IDS,
                        timeout=2,
                        poll_interval=1,
                        stable_reads=2,
                        retry=lambda value: retried.append(value),
                        job_id="job",
                        job_generation=1,
                        guards=open_guards(),
                        recovery_root=root,
                        launch_fn=lambda *_args: ExitedProcess(),
                        sleep_fn=clock.sleep,
                        monotonic_fn=clock.monotonic,
                    )
                self.assertEqual(
                    caught.exception.reason_token,
                    "valid_resave_quiescence_timeout",
                )
                self.assertEqual(retried, [])


class ContinuationAndRaceTests(RecoveryTestCase):
    def test_source_sha_is_rechecked_immediately_before_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            calls = 0
            retried = []

            def last_moment_change(path, expected):
                nonlocal calls
                calls += 1
                if calls == 5:
                    write_spm(spm, spm_text(stale=False, graph_property="9"))
                return _capture_immutable_snapshot(path, expected)

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda value: retried.append(value),
                    job_id="job",
                    generation=7,
                    guards=open_guards(),
                    capture_fn=last_moment_change,
                )
            self.assertEqual(
                caught.exception.reason_token,
                "source_changed_before_continuation",
            )
            self.assertEqual(retried, [])
            self.assertRegex(
                caught.exception.evidence["after_sha256"],
                r"^[0-9a-f]{64}$",
            )

    def test_preimage_receipt_removed_after_save_blocks_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            calls = 0
            retried = []

            def remove_receipt_after_quiescence(path, expected):
                nonlocal calls
                calls += 1
                captured = _capture_immutable_snapshot(path, expected)
                if calls == 4:
                    next(root.glob("*.receipt.json")).unlink()
                return captured

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda value: retried.append(value),
                    job_id="job",
                    generation=8,
                    guards=open_guards(),
                    capture_fn=remove_receipt_after_quiescence,
                )
            self.assertEqual(
                caught.exception.reason_token,
                "preimage_artifacts_missing_or_unreadable",
            )
            self.assertEqual(retried, [])

    def test_cancel_app_close_and_stale_generation_guards_block_callback(self):
        cases = (
            ("cancelled", "initiating_job_cancelled"),
            ("app_closed", "initiating_app_closed"),
            ("job_stale", "initiating_job_generation_stale"),
        )
        for state_key, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                spm, executable, root = self.make_files(folder)
                state = {}
                retried = []

                def mark_guard_after_launch(_exe, _path):
                    state[state_key] = True

                with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                    self.recover_with_save(
                        spm,
                        executable,
                        root,
                        retry=lambda value: retried.append(value),
                        job_id="job",
                        generation=2,
                        guards=open_guards(state),
                        launch_observer=mark_guard_after_launch,
                    )
                self.assertEqual(caught.exception.reason_token, reason)
                self.assertEqual(retried, [])

    def test_retry_requires_complete_job_generation_and_guard_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            launched = []
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                recover_stale_node_table(
                    spm,
                    executable,
                    TARGET_MESH_IDS,
                    retry=lambda _value: None,
                    recovery_root=root,
                    launch_fn=lambda *_args: launched.append(True),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "continuation_context_incomplete",
            )
            self.assertEqual(launched, [])

    def test_same_job_generation_and_after_sha_is_claimed_exactly_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            original = spm.read_bytes()
            retried = []
            first = self.recover_with_save(
                spm,
                executable,
                root,
                retry=lambda value: retried.append(value) or "ok",
                job_id="job-42",
                generation=3,
                guards=open_guards(),
            )
            self.assertEqual(first["status"], "repaired_reaudited_and_retried_once")
            self.assertEqual(len(retried), 1)

            spm.write_bytes(original)
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda value: retried.append(value),
                    job_id="job-42",
                    generation=3,
                    guards=open_guards(),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "continuation_already_claimed",
            )
            self.assertEqual(len(retried), 1)

    def test_callback_failure_is_claimed_and_never_automatically_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            original = spm.read_bytes()
            attempts = []

            def failing(value):
                attempts.append(value)
                raise RuntimeError("downstream failed")

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=failing,
                    job_id="job",
                    generation=4,
                    guards=open_guards(),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "continuation_callback_failed",
            )
            self.assertEqual(len(attempts), 1)
            spm.write_bytes(original)
            with self.assertRaises(StaleNodeTableRecoveryError) as second:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=failing,
                    job_id="job",
                    generation=4,
                    guards=open_guards(),
                )
            self.assertEqual(
                second.exception.reason_token,
                "continuation_already_claimed",
            )
            self.assertEqual(len(attempts), 1)

    def test_concurrent_or_interrupted_session_lock_fails_closed_repeatedly(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            root.mkdir()
            identity = _capture_immutable_snapshot(
                spm, TARGET_MESH_IDS
            )["source_identity"]
            lock, token = _acquire_session_lock(root, identity)
            launched = []
            try:
                for _iteration in range(20):
                    with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                        recover_stale_node_table(
                            spm,
                            executable,
                            TARGET_MESH_IDS,
                            recovery_root=root,
                            launch_fn=lambda *_args: launched.append(True),
                        )
                    self.assertEqual(
                        caught.exception.reason_token,
                        "recovery_session_already_active",
                    )
            finally:
                _release_session_lock(lock, token)
            self.assertEqual(launched, [])

    def test_simultaneous_lock_contenders_have_one_winner_repeatedly(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            identity = _capture_immutable_snapshot(
                spm, TARGET_MESH_IDS
            )["source_identity"]
            for iteration in range(20):
                with self.subTest(iteration=iteration):
                    barrier = threading.Barrier(2)

                    def contender():
                        barrier.wait()
                        try:
                            lock, token = _acquire_session_lock(root, identity)
                            return "winner", lock, token
                        except StaleNodeTableRecoveryError as exc:
                            return "blocked", exc.reason_token, None

                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = list(executor.map(lambda _value: contender(), range(2)))
                    winners = [row for row in results if row[0] == "winner"]
                    blocked = [row for row in results if row[0] == "blocked"]
                    self.assertEqual(len(winners), 1)
                    self.assertEqual(len(blocked), 1)
                    self.assertEqual(
                        blocked[0][1],
                        "recovery_session_already_active",
                    )
                    _release_session_lock(winners[0][1], winners[0][2])

    def test_already_repaired_does_not_launch_backup_or_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            write_spm(spm, spm_text(stale=False))
            launched = []
            retried = []
            result = recover_stale_node_table(
                spm,
                executable,
                TARGET_MESH_IDS,
                retry=lambda value: retried.append(value),
                job_id="job",
                job_generation=1,
                guards=open_guards(),
                recovery_root=root,
                launch_fn=lambda *_args: launched.append(True),
            )
            self.assertEqual(result["status"], "already_repaired")
            self.assertEqual(result["closure_gate"], "operational_snapshot_valid_only")
            self.assertEqual(launched, [])
            self.assertEqual(retried, [])
            self.assertEqual(list(root.glob("*.preimage.spm")), [])


if __name__ == "__main__":
    unittest.main()
