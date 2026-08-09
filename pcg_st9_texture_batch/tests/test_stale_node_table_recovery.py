"""Race and provenance regressions for stale Node-table recovery."""

import contextlib
import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
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
    _authoring_graph_core_projection_for_version,
    _capture_immutable_snapshot,
    _ensure_preimage_artifacts,
    _legacy_authoring_graph_core_v2_projection,
    _legacy_authoring_graph_core_v3_projection,
    _legacy_authoring_graph_core_v4_projection,
    _legacy_target_binding_fingerprint,
    _preimage_receipt,
    _refresh_session_lock,
    _release_session_lock,
    _resolve_receipt_dialect,
    _resolve_target_scopes,
    _verify_preimage_artifacts,
    build_parser,
    recover_stale_node_table,
    verify_sealed_resave,
)
from pcg_st9_texture_batch.speedtree_modeler_uia import (  # noqa: E402
    SEMANTIC_UIA_CONTRACT,
    SemanticModelerUIAError,
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


def default_disabled_planar_2():
    return (
        "<SplineProperty><Name>Forces:Planar 2</Name>"
        "<Value>0.25</Value><Variance>0</Variance><Enabled>false</Enabled>"
        "<CohesionScale>1</CohesionScale><CohesionOffset>0</CohesionOffset>"
        "<Distribution>0</Distribution><ForceBehaviorID>-1</ForceBehaviorID>"
        "<Relative>true</Relative><CompoundParentSpline Count=\"1\">"
        "<Spline DrawMode=\"false\">"
        "<ControlPoint><X>0</X><Y>1</Y><TangentX>1</TangentX>"
        "<TangentY>0</TangentY><Length>0</Length></ControlPoint>"
        "<ControlPoint><X>1</X><Y>1</Y><TangentX>1</TangentX>"
        "<TangentY>0</TangentY><Length>0</Length></ControlPoint>"
        "</Spline></CompoundParentSpline><ProfileSpline DrawMode=\"false\">"
        "<ControlPoint><X>0</X><Y>0</Y><TangentX>1</TangentX>"
        "<TangentY>0</TangentY><Length>0</Length></ControlPoint>"
        "<ControlPoint><X>1</X><Y>1</Y><TangentX>1</TangentX>"
        "<TangentY>0</TangentY><Length>0</Length></ControlPoint>"
        "</ProfileSpline></SplineProperty>"
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
        '<Material_v8 ID="10"><Preview>'
        f"{volatile}</Preview><StreamPlaceholder><Data>{volatile}</Data>"
        "</StreamPlaceholder><Map Name=\"Color\"><TexFilename>"
        f"{material_filename}</TexFilename><TexEnabled>true</TexEnabled>"
        "</Map><CutoutMeshID>130</CutoutMeshID></Material_v8>"
    )
    return spm_text(stale=stale, volatile=volatile).replace(
        "<Generators>",
        "".join(blocks) + "<Generators>",
        1,
    ).replace("<Assets>", "<Assets>" + material, 1)


def issue102_leaf_material_text(
    *,
    stale,
    material_values,
    mesh_values=None,
    material_ids=(1, 5),
    volatile="one",
):
    mesh_values = mesh_values or (-10, -10, -10, -10)
    properties = []
    for type_index, (material_value, mesh_value) in enumerate(zip(
        material_values,
        mesh_values,
    )):
        properties.extend((
            "<Property>"
            f"<Name>Leaves:Type:{type_index}:Material</Name>"
            f"<Value>{material_value}</Value>"
            "</Property>",
            "<Property>"
            f"<Name>Leaves:Type:{type_index}:Mesh</Name>"
            f"<Value>{mesh_value}</Value>"
            "</Property>",
        ))
    generator = (
        '<Generator Type="Leaf Mesh">'
        "<Name>Sanitized dormant leaf</Name>"
        "<GUID>issue102-dormant-leaf</GUID>"
        "<Hidden>false</Hidden>"
        f"<Properties>{''.join(properties)}</Properties>"
        "</Generator>"
    )
    materials = "".join(
        f'<Material_v8 ID="{material_id}" Name="material-{material_id}">'
        "<Map Name=\"Color\"><TexFilename></TexFilename>"
        "<TexEnabled>false</TexEnabled></Map>"
        "</Material_v8>"
        for material_id in material_ids
    )
    return spm_text(stale=stale, volatile=volatile).replace(
        "<SpeedTree>",
        '<SpeedTree BuildInfo="" OS="Windows" Title=" Modeler 10.1.0 " '
        'Version="8" VersionString="10.1.0 ">',
        1,
    ).replace(
        "<Generators>",
        "<Generators>" + generator,
        1,
    ).replace(
        "<Assets>",
        "<Assets>" + materials,
        1,
    )


def write_spm(path, text):
    path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))


def legacy_receipt(snapshot, expected_mesh_ids, backup_name, *, schema_version=2):
    requested = sorted(expected_mesh_ids)
    target_rows = []
    for row in snapshot["delivery"]["leaf_generator_bindings"]:
        mesh_id = int(row["mesh_id"])
        if mesh_id not in requested:
            continue
        if schema_version == 3 and row.get("graph_visible") is not True:
            continue
        target_rows.append({
            "generator_guid": (
                row["generator_guid"].casefold()
                if schema_version == 2
                else row["generator_guid"].rstrip("=").casefold()
            ),
            "generator_type": row["generator_type"],
            "generator_name": row["generator_name"],
            "slot_prefix": row["slot_prefix"],
            "material_property": row["material_property"],
            "material_id": int(row["material_id"]),
            "mesh_property": row["mesh_property"],
            "mesh_id": mesh_id,
        })
    target_rows.sort(key=lambda row: json.dumps(
        row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8") + b"\n")
    target_expected = (
        requested
        if schema_version == 2
        else sorted({row["mesh_id"] for row in target_rows})
    )
    if schema_version == 2:
        root = ET.fromstring(snapshot["text"])
        membership_guids = sorted({
            str(next((child.text or "" for child in element
                      if child.tag.casefold() == "guid"), "")).strip().casefold()
            for element in root.iter()
            if element.tag.casefold() == "generator"
        } - {""})
        membership_fingerprint = hashlib.sha256((json.dumps(
            membership_guids, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ) + "\n").encode("utf-8")).hexdigest()
        membership_count = len(membership_guids)
    else:
        membership_fingerprint = snapshot["generator_membership_fingerprint"]
        membership_count = snapshot["elementtree"]["generator_count"]
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
            "count": membership_count,
            "fingerprint": membership_fingerprint,
        },
        "required_target_bindings": {
            "contract": "speedtree_required_target_binding_projection",
            "version": 1,
            "expected_mesh_ids": target_expected,
            "binding_count": len(target_rows),
            "fingerprint": hashlib.sha256((json.dumps(
                target_rows, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ) + "\n").encode("utf-8")).hexdigest(),
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
        expected_preimage_raw_sha256=None,
        continuation_commit_lock=None,
        on_continuation_claimed=None,
        modeler_session=None,
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
            expected_preimage_raw_sha256=expected_preimage_raw_sha256,
            continuation_commit_lock=continuation_commit_lock,
            on_continuation_claimed=on_continuation_claimed,
            modeler_session=modeler_session,
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

    def test_issue_13_roots_spline_float_fixture_is_field_and_bound_limited(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "issue_13_tree02_roots_spline_float_reserialization.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        fields = {row["tag"]: row for row in fixture["fields"]}

        def document(
            tangent_x,
            tangent_y,
            *,
            property_name=fixture["property_name"],
            tangent_x_tag="TangentX",
        ):
            points = []
            for index in range(7):
                point_x = tangent_x if index == 6 else "1"
                point_y = tangent_y if index == 6 else "0"
                points.append(
                    "<ControlPoint><X>0</X><Y>1</Y>"
                    f"<{tangent_x_tag}>{point_x}</{tangent_x_tag}>"
                    f"<TangentY>{point_y}</TangentY><Length>0</Length>"
                    "</ControlPoint>"
                )
            return (
                "<SpeedTree><Generators><Generator Type=\"Branch\">"
                "<Name>Roots</Name><GUID>roots-guid</GUID><Hidden>false</Hidden>"
                "<Properties><SplineProperty>"
                f"<Name>{property_name}</Name><Value>0</Value>"
                "<CompoundParentSpline Count=\"1\"><Spline DrawMode=\"false\">"
                + "".join(points)
                + "</Spline></CompoundParentSpline><ProfileSpline DrawMode=\"false\" />"
                "</SplineProperty></Properties></Generator></Generators></SpeedTree>"
            )

        before = document(
            fields["TangentX"]["before"],
            fields["TangentY"]["before"],
        )
        after = document(
            fields["TangentX"]["after"],
            fields["TangentY"]["after"],
        )
        self.assertEqual(
            _authoring_graph_core_projection(before)["fingerprint"],
            _authoring_graph_core_projection(after)["fingerprint"],
        )
        for row in fixture["fields"]:
            self.assertEqual(
                f"{round(float(row['before']), 5):.5f}",
                row["canonical_value"],
            )
            self.assertEqual(
                f"{round(float(row['after']), 5):.5f}",
                row["canonical_value"],
            )

        outside_bound = document(
            fields["TangentX"]["outside_bound"],
            fields["TangentY"]["after"],
        )
        self.assertNotEqual(
            _authoring_graph_core_projection(before)["fingerprint"],
            _authoring_graph_core_projection(outside_bound)["fingerprint"],
        )

        unsupported_property_before = document(
            fields["TangentX"]["before"],
            fields["TangentY"]["before"],
            property_name="Spine:Orientation:Unproven value",
        )
        unsupported_property_after = document(
            fields["TangentX"]["after"],
            fields["TangentY"]["after"],
            property_name="Spine:Orientation:Unproven value",
        )
        self.assertNotEqual(
            _authoring_graph_core_projection(unsupported_property_before)[
                "fingerprint"
            ],
            _authoring_graph_core_projection(unsupported_property_after)[
                "fingerprint"
            ],
        )

        unsupported_field_before = document(
            fields["TangentX"]["before"],
            fields["TangentY"]["before"],
            tangent_x_tag="AuthoredTangentX",
        )
        unsupported_field_after = document(
            fields["TangentX"]["after"],
            fields["TangentY"]["after"],
            tangent_x_tag="AuthoredTangentX",
        )
        self.assertNotEqual(
            _authoring_graph_core_projection(unsupported_field_before)[
                "fingerprint"
            ],
            _authoring_graph_core_projection(unsupported_field_after)[
                "fingerprint"
            ],
        )

    def test_issue_13_black_locast_evidence_separates_delivery_and_maintenance(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "issue_13_black_locast_modeler_recovery_evidence.json"
        )
        fixture_text = fixture_path.read_text(encoding="utf-8")
        evidence = json.loads(fixture_text)

        for forbidden in ("C:\\", "D:\\", "/Users/", "\\Users\\", "PARK"):
            self.assertNotIn(forbidden, fixture_text)
        self.assertFalse(evidence["sanitization"]["contains_raw_spm_or_xml"])
        self.assertFalse(evidence["sanitization"]["contains_absolute_paths"])
        self.assertFalse(evidence["safety_boundary"]["direct_spm_xml_edit"])
        self.assertFalse(evidence["safety_boundary"]["save_automation"])
        self.assertFalse(evidence["safety_boundary"]["automated_keystrokes"])
        self.assertFalse(evidence["safety_boundary"]["automatic_rollback"])
        self.assertFalse(evidence["safety_boundary"]["continuation_authorized"])

        results = evidence["results"]
        tree02 = results["SK_tree_black_locast_02.spm"]
        self.assertEqual(
            tree02["disposition"],
            "read_only_reaudit_no_modeler_action_this_run",
        )
        self.assertFalse(tree02["whole_file_sealed_claim"])

        bush02 = results["SK_bush_black_locast_02.spm"]
        self.assertEqual(bush02["disposition"], "accepted_sealed_resave")
        self.assertTrue(bush02["membership_unchanged"])
        self.assertTrue(bush02["authoring_graph_core_v4_unchanged"])
        self.assertFalse(bush02["after"]["stale"])
        self.assertEqual(bush02["after"]["orphan_owner_count"], 0)
        self.assertEqual(bush02["after"]["orphan_node_count"], 0)
        self.assertTrue(bush02["after"]["required_live_projection_complete"])

        bush03 = results["SK_bush_black_locast_03.spm"]
        self.assertEqual(bush03["disposition"], "rejected_unaccepted_postimage")
        self.assertEqual(
            bush03["stop_reason"],
            "authoring_graph_changed_during_resave",
        )
        self.assertTrue(bush03["stale_node_table_repair_passed"])
        self.assertFalse(bush03["sealed_authoring_contract_passed"])
        self.assertFalse(bush03["canonicalization_accepted"])
        self.assertFalse(bush03["continuation_authorized"])
        self.assertFalse(bush03["after"]["stale"])
        self.assertEqual(bush03["after"]["orphan_owner_count"], 0)
        self.assertEqual(bush03["after"]["orphan_node_count"], 0)
        self.assertTrue(bush03["after"]["required_live_projection_complete"])
        changes = bush03["uncovered_core_changes"]
        self.assertEqual(
            {row["property_name"] for row in changes},
            {f"Leaves:Type:{index}:Material" for index in range(4)},
        )
        self.assertEqual(len(changes), 4)
        for row in changes:
            self.assertEqual(row["xml_node_type"], "Property/Value scalar integer")
            self.assertEqual(row["before"], -1)
            self.assertEqual(row["after"], 0)
            self.assertEqual(row["paired_mesh_before"], -10)
            self.assertEqual(row["paired_mesh_after"], -10)

        for asset in (bush02, bush03):
            for artifact in ("preimage", "receipt"):
                file_name = asset[artifact]["file_name"]
                self.assertNotIn("/", file_name)
                self.assertNotIn("\\", file_name)
        rejected_copy = bush03["rejected_postimage_evidence"]
        self.assertEqual(
            rejected_copy["raw_sha256"], bush03["after"]["raw_sha256"]
        )
        self.assertEqual(rejected_copy["size"], bush03["after"]["size"])
        self.assertTrue(rejected_copy["byte_identical_to_canonical_source_at_capture"])

        tree04 = results["SK_tree_black_locast_04.spm"]
        self.assertEqual(
            tree04["disposition"],
            "not_attempted_after_bush03_stop",
        )
        audit = {
            row["asset_name"]: row for row in evidence["takeover_audit"]
        }
        self.assertEqual(
            tree04["raw_sha256"],
            audit["SK_tree_black_locast_04.spm"]["raw_sha256"],
        )
        self.assertTrue(tree04["stale"])

        investigation = evidence["detached_contract_investigation"]
        self.assertEqual(investigation["issue"], 102)
        self.assertEqual(
            investigation["branch"],
            "codex/issue-102-leaf-material-canonicalization",
        )
        self.assertFalse(investigation["asset_branch_contract_change"])
        self.assertFalse(evidence["stop_disposition"]["next_asset_opened"])
        self.assertTrue(
            evidence["stop_disposition"][
                "canonical_postimage_preserved_but_not_accepted"
            ]
        )

        self.assertEqual(evidence["schema_version"], 2)
        current = evidence["current_resolution_audit"]
        self.assertEqual(current["causal_contract_source_issue"], 174)
        self.assertTrue(current["read_only"])
        self.assertFalse(
            current["prior_two_guid_970_orphan_conclusion_reused"]
        )

        current_source = current["source"]
        self.assertEqual(
            current_source["raw_sha256"],
            "83c7c714d26ed9818874bc65fe1fb3ed73c00d5ae4a63fb9b540c9198c29bdf5",
        )
        self.assertEqual(
            current_source["raw_sha256_before"],
            current_source["raw_sha256_after"],
        )
        self.assertNotEqual(current_source["raw_sha256"], tree04["raw_sha256"])
        self.assertTrue(current_source["bytes_unchanged_by_audit"])
        self.assertTrue(current_source["stale"])
        self.assertEqual(current_source["generator_count"], 85)
        self.assertEqual(current_source["node_table_owner_count"], 88)
        self.assertEqual(current_source["orphan_owner_count"], 5)
        self.assertEqual(current_source["orphan_node_count"], 16842)
        self.assertEqual(current_source["total_node_count"], 39256)
        self.assertTrue(current_source["regex_elementtree_parity"])

        scope = current["scope_authority"]
        required_pairs = {
            tuple(row) for row in scope["required_live_pairs"]
        }
        self.assertEqual(
            required_pairs,
            {
                (2, 79),
                (12, 82),
                (12, 83),
                (12, 84),
                (12, 85),
                (13, 86),
                (13, 87),
            },
        )
        self.assertEqual(
            {tuple(row) for row in scope["authoring_pairs"]},
            required_pairs,
        )
        self.assertTrue(scope["manifests_unchanged_by_audit"])

        pair_rows = current["binding_local_required_live"]
        self.assertEqual(
            {
                (row["material_id"], row["mesh_id"])
                for row in pair_rows
            },
            required_pairs,
        )
        self.assertTrue(all(
            row["required_live_pair_delivered"]
            and row["participating_binding_count"] > 0
            and row["fail_closed_binding_count"] == 0
            and row["binding_local_orphan_ancestor_count"] == 0
            for row in pair_rows
        ))

        operational = current["operational_verdict"]
        self.assertEqual(operational["report_status"], "ok")
        self.assertEqual(operational["item_status"], "ready")
        self.assertEqual(operational["target_status"], "ready")
        self.assertEqual(operational["actions"], [])
        self.assertEqual(operational["assembly_handoff_status"], "ready")
        self.assertEqual(operational["assembly_issue_codes"], [])
        self.assertEqual(operational["assembly_error_codes"], [])
        self.assertTrue(operational["current_live_pairs_all_covered"])
        self.assertTrue(operational["spm_material_mesh_pairs_all_complete"])
        self.assertTrue(operational["fbx_material_mesh_pairs_all_complete"])
        self.assertTrue(operational["legacy_scope_drift_diagnostic_present"])
        self.assertEqual(
            operational["legacy_scope_drift_code"],
            "live_generator_slot_not_declared_exactly_once",
        )
        self.assertEqual(operational["legacy_scope_drift_pair"], [2, 79])
        self.assertFalse(operational["legacy_scope_drift_blocks_handoff"])

        maintenance = current["maintenance_verdict"]
        self.assertTrue(maintenance["saved_node_table_still_stale"])
        self.assertFalse(maintenance["orphan_cleanup_complete"])
        self.assertFalse(
            maintenance["modeler_resave_required_for_current_delivery"]
        )
        self.assertTrue(
            maintenance["modeler_resave_required_for_zero_orphan_asset_hygiene"]
        )

        interaction = current["production_interaction"]
        self.assertFalse(interaction["spm_written"])
        self.assertFalse(interaction["scope_manifest_written"])
        self.assertFalse(interaction["modeler_launched"])
        self.assertFalse(interaction["modeler_terminated"])
        self.assertFalse(interaction["backup_required"])
        self.assertFalse(interaction["receipt_required"])

        disposition = current["issue_disposition"]
        self.assertTrue(disposition["issue_13_operational_acceptance_proven"])
        self.assertTrue(
            disposition[
                "legacy_resave_remedy_superseded_for_delivery_by_issue_174"
            ]
        )
        self.assertTrue(disposition["asset_hygiene_debt_remains_nonblocking"])
        self.assertTrue(disposition["close_issue_13"])

    def test_core_v6_accepts_only_exact_disabled_default_planar_2(self):
        before = spm_text(stale=True)
        planar = default_disabled_planar_2()
        after = spm_text(stale=False).replace(
            "<Properties></Properties></Generator>",
            f"<Properties>{planar}</Properties></Generator>",
            1,
        )
        self.assertEqual(
            _authoring_graph_core_projection(before)["version"],
            6,
        )
        self.assertEqual(
            _legacy_authoring_graph_core_v4_projection(before)["version"],
            4,
        )
        self.assertNotEqual(
            _legacy_authoring_graph_core_v4_projection(before)["fingerprint"],
            _legacy_authoring_graph_core_v4_projection(after)["fingerprint"],
        )
        self.assertEqual(
            _authoring_graph_core_projection(before)["fingerprint"],
            _authoring_graph_core_projection(after)["fingerprint"],
        )

        mutations = {
            "other name": planar.replace("Forces:Planar 2", "Forces:Planar 3", 1),
            "value": planar.replace("<Value>0.25</Value>", "<Value>0.5</Value>", 1),
            "variance": planar.replace("<Variance>0</Variance>", "<Variance>1</Variance>", 1),
            "active": planar.replace("<Enabled>false</Enabled>", "<Enabled>true</Enabled>", 1),
            "cohesion": planar.replace("<CohesionScale>1</CohesionScale>", "<CohesionScale>0.5</CohesionScale>", 1),
            "behavior": planar.replace("<ForceBehaviorID>-1</ForceBehaviorID>", "<ForceBehaviorID>0</ForceBehaviorID>", 1),
            "relative": planar.replace("<Relative>true</Relative>", "<Relative>false</Relative>", 1),
            "compound count": planar.replace('Count="1"', 'Count="2"', 1),
            "compound spline": planar.replace("<Y>1</Y>", "<Y>0.9</Y>", 1),
            "profile": planar.replace(
                '<ProfileSpline DrawMode="false"><ControlPoint><X>0</X>',
                '<ProfileSpline DrawMode="false"><ControlPoint><X>0.1</X>',
                1,
            ),
            "attribute": planar.replace("<SplineProperty>", '<SplineProperty Future="1">', 1),
            "extra child": planar.replace("</SplineProperty>", "<Future>1</Future></SplineProperty>", 1),
            "duplicate": planar + planar,
        }
        baseline = _authoring_graph_core_projection(before)["fingerprint"]
        for name, changed_planar in mutations.items():
            with self.subTest(name=name):
                changed = spm_text(stale=False).replace(
                    "<Properties></Properties></Generator>",
                    f"<Properties>{changed_planar}</Properties></Generator>",
                    1,
                )
                self.assertNotEqual(
                    baseline,
                    _authoring_graph_core_projection(changed)["fingerprint"],
                )

        wrong_ancestry = spm_text(stale=False).replace(
            "</SpeedTree>",
            f"<UnknownRoot>{planar}</UnknownRoot></SpeedTree>",
            1,
        )
        self.assertNotEqual(
            baseline,
            _authoring_graph_core_projection(wrong_ancestry)["fingerprint"],
        )

    def test_core_v6_neutralizes_only_draw_flags_view_bit_0x8(self):
        def with_draw_flags(text, value):
            return text.replace(
                "<SpeedTree>",
                "<SpeedTree><DrawFlags8>"
                f"<DrawFlags>{value}</DrawFlags>"
                "</DrawFlags8>",
                1,
            )

        before = with_draw_flags(spm_text(stale=True), "3732")
        after = with_draw_flags(spm_text(stale=False), "3740")
        self.assertNotEqual(
            _legacy_authoring_graph_core_v4_projection(before)["fingerprint"],
            _legacy_authoring_graph_core_v4_projection(after)["fingerprint"],
        )
        self.assertEqual(
            _authoring_graph_core_projection(before)["fingerprint"],
            _authoring_graph_core_projection(after)["fingerprint"],
        )

        baseline = _authoring_graph_core_projection(before)["fingerprint"]
        for value in ("3741", "3733", " 3740", "not-an-integer"):
            with self.subTest(value=value):
                changed = with_draw_flags(spm_text(stale=False), value)
                self.assertNotEqual(
                    baseline,
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

    def test_issue102_unproven_dormant_leaf_material_transition_is_strict(self):
        before = issue102_leaf_material_text(
            stale=True,
            material_values=(-1, -1, -1, -1),
        )
        after = issue102_leaf_material_text(
            stale=False,
            material_values=(0, 0, 0, 0),
            volatile="two",
        )
        before_projection = _authoring_graph_core_projection(before)
        after_projection = _authoring_graph_core_projection(after)

        self.assertNotEqual(
            before_projection["fingerprint"],
            after_projection["fingerprint"],
        )

        differences = []

        def walk(left, right, path="$", names=()):
            if type(left) is not type(right):
                differences.append((path, left, right, names))
                return
            if isinstance(left, dict):
                next_names = names
                if left.get("tag") == "Property":
                    property_names = [
                        child.get("text")
                        for child in left.get("children", [])
                        if child.get("tag") == "Name"
                    ]
                    next_names = names + tuple(property_names)
                for key in sorted(set(left) | set(right)):
                    if key not in left or key not in right:
                        differences.append((path + "." + key, left, right, next_names))
                    else:
                        walk(left[key], right[key], path + "." + key, next_names)
                return
            if isinstance(left, list):
                if len(left) != len(right):
                    differences.append((path + ".length", len(left), len(right), names))
                for index, (before_item, after_item) in enumerate(zip(left, right)):
                    walk(
                        before_item,
                        after_item,
                        f"{path}[{index}]",
                        names,
                    )
                return
            if left != right:
                differences.append((path, left, right, names))

        walk(before_projection["_rows"], after_projection["_rows"])
        self.assertEqual(len(differences), 4)
        self.assertEqual(
            {(before_value, after_value) for _, before_value, after_value, _ in differences},
            {("-1", "0")},
        )
        self.assertEqual(
            {name for _, _, _, names in differences for name in names},
            {f"Leaves:Type:{type_index}:Material" for type_index in range(4)},
        )

    def test_issue102_active_material_zero_remains_observable(self):
        active_zero = issue102_leaf_material_text(
            stale=False,
            material_values=(0, 5, 5, 5),
            mesh_values=(130, -10, -10, -10),
            material_ids=(0, 5),
        )
        active_five = issue102_leaf_material_text(
            stale=False,
            material_values=(5, 5, 5, 5),
            mesh_values=(130, -10, -10, -10),
            material_ids=(0, 5),
        )

        self.assertNotEqual(
            _authoring_graph_core_projection(active_zero)["fingerprint"],
            _authoring_graph_core_projection(active_five)["fingerprint"],
        )

    def test_issue102_recovery_gate_blocks_unproven_transition(self):
        before = issue102_leaf_material_text(
            stale=True,
            material_values=(-1, -1, -1, -1),
        )
        after = issue102_leaf_material_text(
            stale=False,
            material_values=(0, 0, 0, 0),
            volatile="two",
        )
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, recovery_root = self.make_files(folder)
            write_spm(spm, before)

            with self.assertRaises(StaleNodeTableRecoveryTimeout) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    recovery_root,
                    after_text=after,
                    timeout=3,
                )

        self.assertIn(
            "authoring_graph_changed_during_resave",
            caught.exception.evidence["last_reason_tokens"],
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

    def test_disconnected_orphans_do_not_hide_unconnected_paths(self):
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
                    "generator_connection_all_bindings_planned_inactive",
                )
                self.assertEqual(snapshot["normalization"]["errors"], [])
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
        self.assertEqual(contract["schema_version"], 3)
        self.assertTrue(contract["modeler_auto_save"])
        self.assertEqual(
            contract["modeler_auto_save_mode"],
            "exact_owned_pid_document_menu_uia_invoke",
        )
        self.assertFalse(contract["modeler_process_kill"])
        self.assertFalse(contract["direct_spm_xml_edit"])
        self.assertFalse(contract["ui_input_simulation"])
        self.assertFalse(contract["automatic_rollback"])
        self.assertFalse(contract["stale_false_alone_allows_retry"])
        self.assertTrue(contract["requires_node_table_stale"])
        self.assertTrue(contract["requires_nonzero_orphan_owners"])
        self.assertTrue(contract["requires_nonzero_orphan_nodes"])
        self.assertTrue(contract["requires_complete_sealed_scope"])
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
        self.assertEqual(receipt["schema_version"], 8)
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

    def test_schema5_core_v3_receipt_reaudits_byte_for_byte_under_v5(self):
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
            6,
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
                self.assertEqual(receipt["schema_version"], 8)
                self.assertEqual(
                    receipt["authoring_graph_projection"]["version"], 1
                )
                self.assertEqual(
                    receipt["authoring_graph_core_projection"]["version"], 6
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
                self.assertEqual(
                    receipt["exact_preimage"]["source_spm"],
                    str(spm.resolve(strict=False)),
                )
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

    def test_backup_race_immediately_before_launch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            source_captures = 0
            launches = []

            def capture_then_corrupt_backup(path, expected):
                nonlocal source_captures
                snapshot = _capture_immutable_snapshot(path, expected)
                source_captures += 1
                if source_captures == 2:
                    backup = next(root.glob("*.preimage.spm"))
                    write_spm(
                        backup,
                        spm_text(stale=True, graph_property="tampered"),
                    )
                return snapshot

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    capture_fn=capture_then_corrupt_backup,
                    launch_observer=lambda *_args: launches.append(True),
                )

            self.assertEqual(
                caught.exception.reason_token,
                "preimage_backup_verification_failed",
            )
            self.assertFalse(launches)

    def test_launch_guard_backup_mutation_is_caught_by_final_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            launches = []

            def mutate_backup_and_remain_eligible():
                backup = next(root.glob("*.preimage.spm"))
                write_spm(
                    backup,
                    spm_text(stale=True, graph_property="guard-race"),
                )
                return False

            guards = {
                "is_cancelled": mutate_backup_and_remain_eligible,
                "is_app_open": lambda: True,
                "is_job_current": lambda: True,
            }
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    guards=guards,
                    launch_observer=lambda *_args: launches.append(True),
                )

            self.assertEqual(
                caught.exception.reason_token,
                "preimage_backup_verification_failed",
            )
            self.assertFalse(launches)

    def test_launch_guard_source_mutation_is_caught_by_final_recapture(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            launches = []

            def mutate_source_and_remain_eligible():
                write_spm(
                    spm,
                    spm_text(stale=True, graph_property="guard-source-race"),
                )
                return False

            guards = {
                "is_cancelled": mutate_source_and_remain_eligible,
                "is_app_open": lambda: True,
                "is_job_current": lambda: True,
            }
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    guards=guards,
                    launch_observer=lambda *_args: launches.append(True),
                )

            self.assertEqual(
                caught.exception.reason_token,
                "source_changed_before_modeler_launch",
            )
            self.assertFalse(launches)

    def test_backup_race_immediately_before_continuation_blocks_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            guard_calls = 0
            continuations = []

            def mutate_on_continuation_guard():
                nonlocal guard_calls
                guard_calls += 1
                if guard_calls == 2:
                    backup = next(root.glob("*.preimage.spm"))
                    write_spm(
                        backup,
                        spm_text(stale=True, graph_property="tampered"),
                    )
                return False

            guards = {
                "is_cancelled": mutate_on_continuation_guard,
                "is_app_open": lambda: True,
                "is_job_current": lambda: True,
            }
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda continuation: continuations.append(
                        continuation
                    ),
                    job_id="backup-race",
                    generation=1,
                    guards=guards,
                )

            self.assertEqual(
                caught.exception.reason_token,
                "preimage_backup_verification_failed",
            )
            self.assertFalse(continuations)
            self.assertFalse(list(root.glob("continuation.*.claim.json")))

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
        self.assertTrue(result["reaudit"]["generator_membership_continuity"])

    def test_literal_schema2_raw_guid_spelling_receipt_is_frozen(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            write_spm(spm, spm_text(stale=True).replace(
                "<GUID>g-130</GUID>",
                f"<GUID>{MODELER_GENERATOR_GUID}</GUID>",
            ))
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline, TARGET_MESH_IDS, root
            )
            receipt = legacy_receipt(
                baseline,
                TARGET_MESH_IDS,
                artifacts["backup_path"].name,
                schema_version=2,
            )
            self.assertEqual(
                receipt["generator_membership"]["fingerprint"],
                "fa6b5bdea9ca952d5dcde635a1aa06d1e0af05a238f57b0a4a2ef58d2d51fb98",
            )
            self.assertEqual(
                receipt["required_target_bindings"],
                {
                    "contract": "speedtree_required_target_binding_projection",
                    "version": 1,
                    "expected_mesh_ids": [130, 131, 132, 133],
                    "binding_count": 4,
                    "fingerprint": "0c2fe78c6673439216abb63416d15265c354456edd49a197d6a2a8b630854f30",
                },
            )
            artifacts["receipt_path"].write_text(
                json.dumps(receipt, sort_keys=True), encoding="utf-8"
            )
            receipt_bytes = artifacts["receipt_path"].read_bytes()
            frozen = {
                **artifacts,
                "receipt": receipt,
                "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            }
            _verify_preimage_artifacts(frozen, baseline)

            tampered = json.loads(json.dumps(receipt))
            tampered["generator_membership"]["fingerprint"] = baseline[
                "generator_membership_fingerprint"
            ]
            artifacts["receipt_path"].write_text(
                json.dumps(tampered, sort_keys=True), encoding="utf-8"
            )
            tampered_bytes = artifacts["receipt_path"].read_bytes()
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                _verify_preimage_artifacts({
                    **artifacts,
                    "receipt": tampered,
                    "receipt_sha256": hashlib.sha256(tampered_bytes).hexdigest(),
                }, baseline)
            self.assertEqual(
                caught.exception.reason_token,
                "preimage_receipt_verification_failed",
            )

    def test_literal_schema3_target_v1_seals_visible_projection_not_request(self):
        hidden = "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>false</Hidden>"
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            root.mkdir()
            write_spm(spm, spm_text(stale=True).replace(
                hidden,
                "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>true</Hidden>",
                1,
            ))
            baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
            artifacts = _ensure_preimage_artifacts(
                baseline, TARGET_MESH_IDS, root
            )
            receipt = legacy_receipt(
                baseline,
                TARGET_MESH_IDS,
                artifacts["backup_path"].name,
                schema_version=3,
            )
            self.assertEqual(
                baseline["target_projection"]["requested_mesh_ids"],
                [130, 131, 132, 133],
            )
            self.assertEqual(
                receipt["required_target_bindings"],
                {
                    "contract": "speedtree_required_target_binding_projection",
                    "version": 1,
                    "expected_mesh_ids": [130, 131, 132],
                    "binding_count": 3,
                    "fingerprint": "e1e07f589c2da4e9928e72d3be3fec48b2ed8cf03190d072132a0382584be0c2",
                },
            )
            artifacts["receipt_path"].write_text(
                json.dumps(receipt, sort_keys=True), encoding="utf-8"
            )
            receipt_bytes = artifacts["receipt_path"].read_bytes()
            _verify_preimage_artifacts({
                **artifacts,
                "receipt": receipt,
                "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            }, baseline)
            reused = _ensure_preimage_artifacts(
                baseline, TARGET_MESH_IDS, root
            )
            self.assertEqual(reused["receipt_path"].read_bytes(), receipt_bytes)

            write_spm(spm, spm_text(stale=False, volatile="two").replace(
                hidden,
                "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>true</Hidden>",
                1,
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
                "preimage_reaudit_failed",
            )
            sealed_backup_bytes = artifacts["backup_path"].read_bytes()
            sealed_receipt_bytes = artifacts["receipt_path"].read_bytes()
            write_spm(spm, spm_text(stale=True).replace(
                hidden,
                "<Name>Leaf 133</Name><GUID>g-133</GUID><Hidden>true</Hidden>",
                1,
            ))
            launches = []
            claims = []
            with self.assertRaises(StaleNodeTableRecoveryError) as recovery_error:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda continuation: claims.append(continuation),
                    job_id="schema3-projected-subset",
                    generation=1,
                    guards=open_guards(),
                    launch_observer=lambda *_args: launches.append(True),
                )
            self.assertEqual(
                recovery_error.exception.reason_token,
                "preimage_target_manifest_incomplete",
            )
            self.assertEqual(launches, [])
            self.assertEqual(claims, [])
            self.assertEqual(
                artifacts["backup_path"].read_bytes(), sealed_backup_bytes
            )
            self.assertEqual(
                artifacts["receipt_path"].read_bytes(), sealed_receipt_bytes
            )
            self.assertFalse(list(root.glob("continuation.*.claim.json")))

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

    def test_schema3_core_v2_is_rebuilt_before_current_v5_projection(self):
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
            6,
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

    def test_same_mesh_one_live_three_dead_siblings_satisfy_pair_delivery(self):
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

            result = self.recover_with_save(
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
        self.assertEqual(result["status"], "repaired_reaudit_valid")
        self.assertEqual(
            result["reaudit"]["target_delivery"]["errors"],
            [],
        )
        self.assertTrue(result["reaudit"]["normalization"]["complete"])

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


class Schema7PathCompatibilityTests(RecoveryTestCase):
    def make_schema7_artifacts(self, folder, *, absolute_texture):
        spm = folder / "model.spm"
        backup = folder / "model.preimage.spm"
        receipt_path = folder / "model.receipt.json"
        before = authored_scope_text(
            stale=True,
            guid_suffix="schema7",
            volatile="before",
            material_filename=str(absolute_texture),
        )
        write_spm(spm, before)
        baseline = _capture_immutable_snapshot(spm, TARGET_MESH_IDS)
        target_scopes, error = _resolve_target_scopes(TARGET_MESH_IDS)
        self.assertIsNone(error)
        receipt = _preimage_receipt(
            baseline,
            target_scopes,
            backup.name,
        )
        receipt["schema_version"] = 7
        receipt["exact_preimage"].pop("source_spm")
        legacy_core = _authoring_graph_core_projection_for_version(before, 5)
        receipt["authoring_graph_core_projection"] = {
            key: value
            for key, value in legacy_core.items()
            if not key.startswith("_")
        }
        backup.write_bytes(spm.read_bytes())
        receipt_path.write_text(
            json.dumps(receipt, sort_keys=True),
            encoding="utf-8",
        )
        return spm, backup, receipt_path

    def test_schema7_core5_receipt_accepts_only_same_resolved_texture_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            texture = folder / "textures" / "leaf.png"
            texture.parent.mkdir()
            texture.write_bytes(b"fixture")
            spm, backup, receipt = self.make_schema7_artifacts(
                folder,
                absolute_texture=texture,
            )
            after = authored_scope_text(
                stale=False,
                guid_suffix="schema7",
                volatile="after",
                material_filename=str(Path("textures") / "leaf.png"),
            )
            write_spm(spm, after)

            result = verify_sealed_resave(
                spm,
                backup,
                receipt,
                TARGET_MESH_IDS,
            )

            self.assertEqual(result["status"], "sealed_resave_reaudit_valid")
            self.assertEqual(
                result["reaudit"]["authoring_graph_core_projection_version"],
                6,
            )

    def test_schema7_core5_receipt_rejects_relative_retarget(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            texture = folder / "textures" / "leaf.png"
            texture.parent.mkdir()
            texture.write_bytes(b"fixture")
            spm, backup, receipt = self.make_schema7_artifacts(
                folder,
                absolute_texture=texture,
            )
            after = authored_scope_text(
                stale=False,
                guid_suffix="schema7",
                volatile="after",
                material_filename=str(Path("other") / "leaf.png"),
            )
            write_spm(spm, after)

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                verify_sealed_resave(
                    spm,
                    backup,
                    receipt,
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


class SemanticUIARecoveryTests(RecoveryTestCase):
    @staticmethod
    def semantic_receipt(path, operation, *, reused=False):
        return {
            "contract": SEMANTIC_UIA_CONTRACT,
            "owned_process_id": 4242,
            "document_accessible_name": Path(path).name,
            "operation": operation,
            "menu_path": [
                "File",
                "Save" if operation == "save" else "Close",
            ],
            "semantic_pattern": "InvokePattern",
            "bridge_exit_code": 0,
            "session_reused": reused,
            "owned_process_alive_after_invoke": True,
        }

    def test_stale_orphan_target_invokes_save_once_then_closes_after_reaudit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            calls = []
            after_text = spm_text(stale=False, volatile="semantic")

            class Session:
                def save_document(inner_self, observed_executable, observed_spm):
                    calls.append(("save", Path(observed_executable), Path(observed_spm)))
                    write_spm(spm, after_text)
                    return self.semantic_receipt(observed_spm, "save")

                def close_document(inner_self, observed_spm):
                    calls.append(("close", Path(observed_spm)))
                    receipt = self.semantic_receipt(observed_spm, "close")
                    receipt["exact_document_closed"] = True
                    receipt["owned_process_alive_after_close"] = True
                    return receipt

            result = self.recover_with_save(
                spm,
                executable,
                root,
                modeler_session=Session(),
            )

            self.assertEqual([row[0] for row in calls], ["save", "close"])
            self.assertTrue(result["reaudit"]["valid"])
            node_table = result["reaudit"]["target_delivery"]["node_table"]
            self.assertFalse(node_table["stale"])
            self.assertEqual(node_table["orphan_node_count"], 0)
            completion = result["semantic_completion_receipt"]
            completion_path = root / completion["file"]
            self.assertTrue(completion_path.is_file())
            payload = json.loads(completion_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["preimage"]["node_table_stale"], True)
            self.assertGreater(payload["preimage"]["orphan_generator_guid_count"], 0)
            self.assertGreater(payload["preimage"]["orphan_node_count"], 0)
            self.assertEqual(payload["postimage"]["node_table_stale"], False)
            self.assertEqual(payload["postimage"]["orphan_generator_guid_count"], 0)
            self.assertEqual(payload["postimage"]["orphan_node_count"], 0)
            self.assertTrue(payload["postimage"]["authoring_graph_continuity"])
            self.assertTrue(payload["postimage"]["generator_membership_continuity"])
            self.assertTrue(payload["postimage"]["required_target_binding_continuity"])
            self.assertEqual(payload["semantic_uia"]["save"]["owned_process_id"], 4242)
            self.assertEqual(payload["semantic_uia"]["close"]["menu_path"], ["File", "Close"])

    def test_zero_orphan_stale_evidence_never_invokes_semantic_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            calls = []

            def zero_orphan_capture(path, expected):
                snapshot = _capture_immutable_snapshot(path, expected)
                node_table = snapshot["delivery"]["node_table"]
                node_table["stale"] = True
                node_table["orphan_generator_guids"] = []
                node_table["orphan_node_count"] = 0
                return snapshot

            class Session:
                def save_document(inner_self, *_args):
                    calls.append("save")

                def close_document(inner_self, *_args):
                    calls.append("close")

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    capture_fn=zero_orphan_capture,
                    modeler_session=Session(),
                )

            self.assertEqual(
                caught.exception.reason_token,
                "stale_orphan_evidence_missing",
            )
            self.assertEqual(calls, [])
            self.assertEqual(list(root.glob("*.preimage.spm")), [])

    def test_ambiguous_document_blocks_without_close_or_source_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            before = spm.read_bytes()
            calls = []

            class Session:
                def save_document(inner_self, _executable, observed_spm):
                    calls.append("save")
                    raise SemanticModelerUIAError(
                        "uia_document_ambiguous",
                        "fixture ambiguity",
                        {
                            "owned_process_id": 4242,
                            "document_accessible_name": Path(observed_spm).name,
                            "operation": "save",
                        },
                    )

                def close_document(inner_self, *_args):
                    calls.append("close")

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    modeler_session=Session(),
                )

            self.assertEqual(caught.exception.reason_token, "uia_document_ambiguous")
            self.assertEqual(calls, ["save"])
            self.assertEqual(spm.read_bytes(), before)
            blocked = json.loads(
                (root / caught.exception.evidence["blocked_event"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(blocked["semantic_uia"]["owned_process_id"], 4242)
            self.assertEqual(blocked["semantic_uia"]["operation"], "save")

    def test_interruption_runs_bounded_session_cleanup_and_records_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            state = {}
            calls = []

            class Session:
                def save_document(inner_self, _executable, observed_spm):
                    calls.append("save")
                    state["cancelled"] = True
                    return self.semantic_receipt(observed_spm, "save")

                def close_document(inner_self, _observed_spm):
                    calls.append("close")

                def cleanup_after_failure(inner_self, observed_spm):
                    calls.append(("cleanup", Path(observed_spm).name))
                    return {
                        "cleanup_status": "owned_process_exited_gracefully",
                        "owned_process_id": 4242,
                        "exact_document_closed": True,
                        "force_termination_used": False,
                    }

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    guards=open_guards(state),
                    modeler_session=Session(),
                )

            self.assertEqual(caught.exception.reason_token, "initiating_job_cancelled")
            self.assertEqual(calls, ["save", ("cleanup", "model.spm")])
            cleanup = caught.exception.evidence["semantic_uia_cleanup"]
            self.assertEqual(cleanup["owned_process_id"], 4242)
            self.assertFalse(cleanup["force_termination_used"])
            blocked = json.loads(
                (root / caught.exception.evidence["blocked_event"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                blocked["semantic_uia_cleanup"]["cleanup_status"],
                "owned_process_exited_gracefully",
            )


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
    def test_claim_hook_and_stop_commit_share_one_linearization_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            order = []

            class ObservedLock:
                def __init__(self):
                    self.lock = threading.RLock()
                    self.inside = False

                def __enter__(self):
                    self.lock.acquire()
                    self.inside = True
                    return self

                def __exit__(self, *_args):
                    self.inside = False
                    self.lock.release()

            commit_lock = ObservedLock()

            def claimed(_payload):
                self.assertTrue(commit_lock.inside)
                order.append("claim")

            def retry(_continuation):
                self.assertFalse(commit_lock.inside)
                order.append("retry")
                return "resumed"

            result = self.recover_with_save(
                spm,
                executable,
                root,
                retry=retry,
                job_id="linearized",
                generation=1,
                guards=open_guards(),
                continuation_commit_lock=commit_lock,
                on_continuation_claimed=claimed,
            )

            self.assertEqual(order, ["claim", "retry"])
            self.assertEqual(result["retry_result"], "resumed")

    def test_cancel_during_unchanged_manual_wait_stops_before_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            clock = FakeClock()
            state = {}

            def cancel_after_first_poll(seconds):
                clock.sleep(seconds)
                state["cancelled"] = True

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                recover_stale_node_table(
                    spm,
                    executable,
                    TARGET_MESH_IDS,
                    timeout=7200,
                    poll_interval=1,
                    stable_reads=2,
                    retry=lambda _value: self.fail(
                        "cancelled wait must not resume"
                    ),
                    job_id="mid-wait-cancel",
                    job_generation=1,
                    guards=open_guards(state),
                    recovery_root=root,
                    launch_fn=lambda *_args: ExitedProcess(),
                    sleep_fn=cancel_after_first_poll,
                    monotonic_fn=clock.monotonic,
                )

            self.assertEqual(
                caught.exception.reason_token,
                "initiating_job_cancelled",
            )
            self.assertEqual(clock.now, 1.0)
            self.assertFalse(list(root.glob("continuation.*.claim.json")))

    def test_queue_lease_loss_during_manual_wait_stops_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            clock = FakeClock()
            state = {"queue_current": True}
            guards = open_guards(state)
            guards["is_queue_current"] = lambda: state["queue_current"]

            def lose_lease_after_first_poll(seconds):
                clock.sleep(seconds)
                state["queue_current"] = False

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                recover_stale_node_table(
                    spm,
                    executable,
                    TARGET_MESH_IDS,
                    timeout=7200,
                    poll_interval=1,
                    stable_reads=2,
                    retry=lambda _value: self.fail(
                        "lost lease must not resume"
                    ),
                    job_id="mid-wait-lease-loss",
                    job_generation=1,
                    guards=guards,
                    recovery_root=root,
                    launch_fn=lambda *_args: ExitedProcess(),
                    sleep_fn=lose_lease_after_first_poll,
                    monotonic_fn=clock.monotonic,
                )

            self.assertEqual(
                caught.exception.reason_token,
                "initiating_queue_lease_lost",
            )
            self.assertEqual(clock.now, 1.0)

    def test_blocking_audit_sha_mismatch_prevents_modeler_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            launches = []
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    expected_preimage_raw_sha256="0" * 64,
                    launch_observer=lambda *_args: launches.append(True),
                )

            self.assertEqual(
                caught.exception.reason_token,
                "audit_preimage_identity_changed",
            )
            self.assertFalse(launches)

    def test_continuation_guard_source_mutation_blocks_claim_and_callback(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, executable, root = self.make_files(folder)
            guard_calls = 0
            continuations = []

            def mutate_on_continuation_guard():
                nonlocal guard_calls
                guard_calls += 1
                # Prelaunch plus manual-wait polling consume four checks
                # before the final continuation authority check.
                if guard_calls == 5:
                    write_spm(
                        spm,
                        spm_text(stale=False, graph_property="guard-source-race"),
                    )
                return False

            guards = {
                "is_cancelled": mutate_on_continuation_guard,
                "is_app_open": lambda: True,
                "is_job_current": lambda: True,
            }
            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                self.recover_with_save(
                    spm,
                    executable,
                    root,
                    retry=lambda continuation: continuations.append(continuation),
                    job_id="continuation-source-race",
                    generation=1,
                    guards=guards,
                )

            self.assertEqual(
                caught.exception.reason_token,
                "source_changed_before_continuation",
            )
            self.assertEqual(continuations, [])
            self.assertFalse(list(root.glob("continuation.*.claim.json")))

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

    def test_session_lock_seals_pid_start_time_and_monotonic_heartbeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            identity = _capture_immutable_snapshot(
                spm, TARGET_MESH_IDS
            )["source_identity"]
            lock, token = _acquire_session_lock(root, identity)
            try:
                initial = json.loads(lock.read_text(encoding="utf-8"))
                self.assertEqual(initial["schema_version"], 2)
                self.assertEqual(initial["owner_pid"], os.getpid())
                self.assertTrue(initial["owner_process_start_identity"])
                self.assertIn("owner_process_started_at_utc", initial)
                self.assertEqual(initial["heartbeat_sequence"], 0)
                self.assertIsInstance(
                    initial["heartbeat_monotonic_seconds"],
                    (int, float),
                )
                refreshed = _refresh_session_lock(lock, token)
                self.assertEqual(refreshed["heartbeat_sequence"], 1)
                self.assertGreaterEqual(
                    refreshed["heartbeat_monotonic_seconds"],
                    initial["heartbeat_monotonic_seconds"],
                )
            finally:
                _release_session_lock(lock, token)

    def test_dead_interrupted_owner_is_reclaimed_but_live_owner_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            identity = _capture_immutable_snapshot(
                spm, TARGET_MESH_IDS
            )["source_identity"]
            lock, old_token = _acquire_session_lock(
                root,
                identity,
                owner_pid=4100,
                owner_process_start_identity="creation-old",
            )
            try:
                with self.assertRaises(StaleNodeTableRecoveryError) as active:
                    _acquire_session_lock(
                        root,
                        identity,
                        owner_pid=4200,
                        owner_process_start_identity="creation-new",
                        liveness_fn=lambda pid, start: True,
                    )
                self.assertEqual(
                    active.exception.reason_token,
                    "recovery_session_already_active",
                )
                self.assertEqual(
                    json.loads(lock.read_text(encoding="utf-8"))["session_token"],
                    old_token,
                )

                new_lock, new_token = _acquire_session_lock(
                    root,
                    identity,
                    owner_pid=4200,
                    owner_process_start_identity="creation-new",
                    liveness_fn=lambda pid, start: False,
                )
                current = json.loads(new_lock.read_text(encoding="utf-8"))
                self.assertEqual(current["owner_pid"], 4200)
                self.assertEqual(
                    current["owner_process_start_identity"],
                    "creation-new",
                )
                self.assertNotEqual(current["session_token"], old_token)
                self.assertEqual(len(list(root.glob("reclaimed.session.*.json"))), 1)
            finally:
                _release_session_lock(
                    lock,
                    locals().get("new_token", old_token),
                    owner_pid=4200 if "new_token" in locals() else 4100,
                    owner_process_start_identity=(
                        "creation-new" if "new_token" in locals() else "creation-old"
                    ),
                )

    def test_pid_reuse_does_not_make_the_interrupted_owner_look_alive(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            identity = _capture_immutable_snapshot(
                spm, TARGET_MESH_IDS
            )["source_identity"]
            lock, old_token = _acquire_session_lock(
                root,
                identity,
                owner_pid=5100,
                owner_process_start_identity="creation-before-reuse",
            )
            observed = []

            def reused_pid_is_not_exact_owner(pid, start_identity):
                observed.append((pid, start_identity))
                # PID 5100 exists again, but its creation identity differs.
                return False

            new_lock, new_token = _acquire_session_lock(
                root,
                identity,
                owner_pid=5200,
                owner_process_start_identity="creation-recovery",
                liveness_fn=reused_pid_is_not_exact_owner,
            )
            try:
                self.assertEqual(
                    observed,
                    [(5100, "creation-before-reuse")],
                )
                current = json.loads(new_lock.read_text(encoding="utf-8"))
                self.assertEqual(current["session_token"], new_token)
                self.assertNotEqual(current["session_token"], old_token)
            finally:
                _release_session_lock(
                    new_lock,
                    new_token,
                    owner_pid=5200,
                    owner_process_start_identity="creation-recovery",
                )

    def test_legacy_or_malformed_lock_is_never_stolen_without_owner_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            spm, _executable, root = self.make_files(folder)
            root.mkdir()
            identity = _capture_immutable_snapshot(
                spm, TARGET_MESH_IDS
            )["source_identity"]
            lock = root / (
                "session."
                + identity["source_identity_sha256"][:24]
                + ".lock.json"
            )
            legacy = {
                "kind": "speedtree_stale_node_table_recovery_session_lock",
                "schema_version": 1,
                **identity,
                "session_token": "a" * 32,
            }
            lock.write_text(json.dumps(legacy), encoding="utf-8")

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                _acquire_session_lock(
                    root,
                    identity,
                    owner_pid=6200,
                    owner_process_start_identity="creation-new",
                    liveness_fn=lambda _pid, _start: False,
                )

            self.assertEqual(
                caught.exception.reason_token,
                "recovery_session_lock_ownership_unverifiable",
            )
            self.assertEqual(
                json.loads(lock.read_text(encoding="utf-8")),
                legacy,
            )

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
