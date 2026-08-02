"""Sanitized integration regression for issue #40's Densiflora recurrence."""

import gzip
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from pcg_cluster_assembly_contract import (  # noqa: E402
    _delivery_binding_slot_identity,
    _normalized_generator_delivery,
)
from generator_delivery_scope import (  # noqa: E402
    CONTINUITY_ONLY_POLICY,
    INTENT_KIND,
    RESOLVED_KIND,
    RUNTIME_INACTIVE_POLICY,
    SCOPE_KIND,
    canonical_sha256,
)
from stale_node_table_recovery import (  # noqa: E402
    StaleNodeTableRecoveryError,
    _capture_immutable_snapshot,
    recover_stale_node_table,
    validate_repaired_snapshot,
)


FIXTURE = Path(__file__).parent / "fixtures" / (
    "issue40_densiflora_live_recurrence.json"
)
WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:(?<![a-z])[a-z]:[\\/]|users[\\/][^\\/]+)"
)
RAW_GUID_RE = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _material_xml(material):
    primary, *supplemental = material["mesh_ids"]
    supplemental_xml = "".join(
        f'<CutoutMesh ID="{mesh_id}"/>' for mesh_id in supplemental
    )
    return (
        f'<Material_v8 ID="{material["material_id"]}" '
        f'Name="{material["material_name"]}">'
        f"<CutoutMeshID>{primary}</CutoutMeshID>"
        f'<SupplementalCutoutMeshIDs Count="{len(supplemental)}">'
        f"{supplemental_xml}</SupplementalCutoutMeshIDs>"
        "</Material_v8>"
    )


def _generator_xml(binding):
    hidden = "false" if binding["graph_visible"] else "true"
    return (
        f'<Generator Type="{binding["generator_type"]}">'
        f'<Name>{binding["generator_name"]}</Name>'
        f'<GUID>{binding["generator_guid"]}</GUID>'
        f"<Hidden>{hidden}</Hidden><Properties>"
        "<Property>"
        f'<Name>{binding["slot_prefix"]}:Material</Name>'
        f'<Value>{binding["material_id"]}</Value>'
        "</Property><Property>"
        f'<Name>{binding["slot_prefix"]}:Mesh</Name>'
        f'<Value>{binding["mesh_id"]}</Value>'
        "</Property></Properties></Generator>"
    )


def _node_xml(generator_guid, ordinal):
    return (
        "<Node>"
        f"<GeneratorGUID>{generator_guid}</GeneratorGUID>"
        "<ParentGUID></ParentGUID>"
        f"<Name>fixture-node-{ordinal}</Name>"
        f"<GUID>fixture-node-guid-{ordinal}</GUID>"
        "<Hidden>false</Hidden>"
        "<Extra><m_bDeleted>false</m_bDeleted>"
        "<m_bCulled>false</m_bCulled></Extra>"
        "</Node>"
    )


def write_fixture_spm(path, fixture):
    bindings = fixture["generator_bindings"]
    material = fixture["material"]
    generators = [
        '<Generator Type="Tree"><Name>Tree</Name><GUID>fixture-root</GUID>'
        "<Hidden>false</Hidden><Properties></Properties></Generator>"
    ]
    generators.extend(_generator_xml(binding) for binding in bindings)
    links = "".join(
        "<Link><SourceGUID>fixture-root</SourceGUID>"
        f'<TargetGUID>{binding["generator_guid"]}</TargetGUID></Link>'
        for binding in bindings
    )
    nodes = []
    ordinal = 0
    for binding in bindings:
        for _ in range(binding["generated_node_count"]):
            ordinal += 1
            nodes.append(_node_xml(binding["generator_guid"], ordinal))
    meshes = "".join(
        f'<Mesh ID="{mesh_id}" Name="fixture-mesh-{mesh_id}"/>'
        for mesh_id in material["mesh_ids"]
    )
    payload = (
        "<SpeedTree><Assets>"
        + _material_xml(material)
        + "<Meshes>"
        + meshes
        + "</Meshes></Assets><Generators>"
        + "".join(generators)
        + "</Generators><Links>"
        + links
        + "</Links><Nodes>"
        + "".join(nodes)
        + "</Nodes></SpeedTree>"
    )
    path.write_bytes(gzip.compress(payload.encode("utf-8"), mtime=0))


def declared_delivery_payload(fixture, target, snapshot_sha256):
    bindings = [
        {
            "state": "already_connected",
            "generator_index": index,
            "generator_name": binding["generator_name"],
            "generator_guid": binding["generator_guid"],
            "generator_type": binding["generator_type"],
            "slot_prefix": binding["slot_prefix"],
            "target_material_id": binding["material_id"],
            "target_mesh_id": binding["mesh_id"],
        }
        for index, binding in enumerate(fixture["generator_bindings"])
    ]
    authored = [
        {
            "slot_identity": list(_delivery_binding_slot_identity(binding)),
            "target_material_id": binding["target_material_id"],
            "target_mesh_id": binding["target_mesh_id"],
        }
        for binding in bindings
    ]
    required_mesh_ids = set(
        fixture["recovery_scope_contract"]["required_live_mesh_ids"]
    )
    required = [
        row["slot_identity"]
        for row in authored
        if row["target_mesh_id"] in required_mesh_ids
    ]
    continuity = [
        {
            "slot_identity": row["slot_identity"],
            "reason": "sanitized Densiflora authored continuity",
            "policy": CONTINUITY_ONLY_POLICY,
            "provenance": {
                "fixture": "issue40_densiflora_live_recurrence",
                "authority_revision": 1,
            },
        }
        for row in authored
        if row["target_mesh_id"] not in required_mesh_ids
    ]
    provider_blend = str(Path("sanitized-provider.blend").resolve())
    intent = {
        "kind": INTENT_KIND,
        "schema_version": 1,
        "authority": {
            "kind": "sanitized_test_recipe",
            "id": "issue-40-densiflora",
            "provenance": {"fixture": "four-authored-one-live"},
        },
        "target": {
            "spm": str(Path(target).resolve()),
            "provider_blend": provider_blend,
            "provider_scope_id": "densiflora-cluster-01",
            "material_id": fixture["material"]["material_id"],
        },
        "authored_slots": authored,
        "required_live_slot_identities": required,
        "continuity_only_slots": continuity,
        "runtime_inactive_policy": RUNTIME_INACTIVE_POLICY,
    }
    intent["intent_sha256"] = canonical_sha256(intent)
    resolved = {
        "kind": RESOLVED_KIND,
        "schema_version": 1,
        "intent_sha256": intent["intent_sha256"],
        "bindings_sha256": canonical_sha256(authored),
        "target_spm_postwrite_sha256": snapshot_sha256,
    }
    resolved["resolved_sha256"] = canonical_sha256(resolved)
    return {
        "blend_file": provider_blend,
        "generator_connection": {
            "requested": True,
            "complete": True,
            "generator_variant_policy": "ensure_all_material_cutouts",
            "bindings": bindings,
            "delivery_scope": {
                "kind": SCOPE_KIND,
                "schema_version": 1,
                "intent": intent,
                "resolved": resolved,
            },
        },
    }


class DensifloraLiveRecurrenceTests(unittest.TestCase):
    def test_fixture_is_sanitized_and_keeps_issue_40_separate_from_58(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        fixture = json.loads(raw)
        manifest_boundary = fixture["manifest_resolution_boundary"]
        scope_contract = fixture["recovery_scope_contract"]
        strict_audit = fixture["legacy_strict_audit"]

        self.assertEqual(fixture["schema_version"], 1)
        self.assertEqual(fixture["source_issue"], 40)
        self.assertIsNone(WINDOWS_PATH_RE.search(raw))
        self.assertIsNone(RAW_GUID_RE.search(raw))
        self.assertEqual(manifest_boundary["source_issue"], 58)
        self.assertEqual(scope_contract["source_issue"], 40)
        self.assertTrue(
            manifest_boundary["candidate_resolution_is_prerequisite"]
        )
        self.assertEqual(
            manifest_boundary["selected_declared_mesh_ids"],
            strict_audit["declared_mesh_ids"],
        )
        self.assertEqual(
            manifest_boundary["selected_declared_mesh_ids"],
            scope_contract["authoring_mesh_ids"],
        )
        self.assertEqual(
            strict_audit["live_export_participating_mesh_ids"],
            scope_contract["required_live_mesh_ids"],
        )
        self.assertNotEqual(
            manifest_boundary["selected_declared_mesh_ids"],
            scope_contract["required_live_mesh_ids"],
        )
        self.assertTrue(
            scope_contract["post_manifest_selection_scope_split_remains"]
        )

    def test_explicit_delivery_scope_eliminates_the_live_recurrence(self):
        fixture = load_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / fixture["target_spm"]
            write_fixture_spm(target, fixture)
            snapshot = _capture_immutable_snapshot(
                target,
                fixture["recovery_scope_contract"]["authoring_mesh_ids"],
            )["delivery"]

            class FrozenAudit:
                @staticmethod
                def live_generator_delivery_snapshot(_spm):
                    return snapshot

            delivery = _normalized_generator_delivery(
                FrozenAudit,
                target,
                declared_delivery_payload(
                    fixture,
                    target,
                    snapshot["spm_text_sha256"],
                ),
                {"material_id": fixture["material"]["material_id"]},
                [
                    {"target_mesh_id": mesh_id}
                    for mesh_id in fixture["material"]["mesh_ids"]
                ],
            )

        expected = fixture["recovery_scope_contract"]
        self.assertEqual(delivery["delivery_scope_mode"], "explicit_sealed_v1")
        self.assertEqual(
            delivery["normalized_target_mesh_ids"],
            expected["authoring_mesh_ids"],
        )
        self.assertEqual(
            delivery["current_required_target_mesh_ids"],
            expected["required_live_mesh_ids"],
        )
        recovery_scope = delivery["recovery_target_scope"]
        self.assertEqual(
            recovery_scope["authoring_mesh_ids"],
            expected["authoring_mesh_ids"],
        )
        self.assertEqual(
            recovery_scope["required_live_mesh_ids"],
            expected["required_live_mesh_ids"],
        )
        self.assertRegex(recovery_scope["scope_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            delivery["live_export_participating_target_mesh_ids"],
            expected["required_live_mesh_ids"],
        )
        self.assertEqual(delivery["errors"], [])
        self.assertTrue(delivery["live_generator_delivery_complete"])

    def test_explicit_scope_accepts_the_fresh_authored_subset_shape(self):
        fixture = load_fixture()
        scopes = fixture["recovery_scope_contract"]
        authoring = scopes["authoring_mesh_ids"]
        required_live = scopes["required_live_mesh_ids"]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            target = folder / fixture["target_spm"]
            executable = folder / "SpeedTree_Modeler.exe"
            executable.write_bytes(b"fixture executable")
            write_fixture_spm(target, fixture)
            captured = _capture_immutable_snapshot(target, authoring)

            node_table = captured["delivery"]["node_table"]
            self.assertEqual(
                node_table["stale"],
                fixture["node_table"]["stale"],
            )
            self.assertEqual(
                len(node_table["orphan_generator_guids"]),
                fixture["node_table"]["orphan_owner_count"],
            )
            self.assertEqual(
                node_table["orphan_node_count"],
                fixture["node_table"]["orphan_node_count"],
            )
            self.assertEqual(
                node_table["total_node_count"],
                fixture["node_table"]["total_node_count"],
            )
            self.assertEqual(
                captured["target_projection"][
                    "all_binding_target_mesh_ids"
                ],
                authoring,
            )
            self.assertEqual(
                captured["target_projection"]["live_export_target_mesh_ids"],
                required_live,
            )

            strict = validate_repaired_snapshot(
                captured["delivery"],
                authoring,
            )
            explicit = validate_repaired_snapshot(
                captured["delivery"],
                authoring,
                required_live,
            )
            self.assertFalse(strict["valid"])
            self.assertIn("live_target_mesh_set_incomplete", strict["errors"])
            self.assertTrue(explicit["valid"])

            def fail_if_launched(_executable, _spm):
                self.fail("fresh explicit-scope fixture must not launch Modeler")

            result = recover_stale_node_table(
                target,
                executable,
                (),
                authoring_mesh_ids=authoring,
                required_live_mesh_ids=required_live,
                recovery_root=folder / "explicit-recovery",
                launch_fn=fail_if_launched,
            )
            self.assertEqual(result["status"], "already_repaired")
            self.assertFalse(result["modeler_launched"])

            with self.assertRaises(StaleNodeTableRecoveryError) as caught:
                recover_stale_node_table(
                    target,
                    executable,
                    authoring,
                    recovery_root=folder / "legacy-strict-recovery",
                    launch_fn=fail_if_launched,
                )

        self.assertEqual(
            caught.exception.reason_token,
            "non_stale_delivery_gate_failed",
        )
        self.assertIn(
            "live_target_mesh_set_incomplete",
            caught.exception.evidence["reason_tokens"],
        )


if __name__ == "__main__":
    unittest.main()
