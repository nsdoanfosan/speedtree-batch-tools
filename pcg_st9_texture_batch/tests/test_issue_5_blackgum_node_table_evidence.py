"""Validate the public, anonymized Issue #5 pre-resave evidence."""

import json
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import pcg_texture_audit as audit  # noqa: E402

FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "issue_5_blackgum_node_table_evidence.json"
)
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GUID_TOKEN_RE = re.compile(r"(?:current|orphan)-[0-9]{3}")
RAW_UUID_RE = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?<![a-z])[a-z]:[\\/]")
USER_HOME_PATH_RE = re.compile(r"(?i)(?:[\\/](?:users|home)[\\/])")


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def relationship_from_anonymized_evidence(before):
    evidence = before["guid_ownership_evidence"]
    current = set(evidence["current_generator_guid_tokens"])
    eligible_by_guid = evidence["eligible_node_count_by_guid_token"]
    eligible_owners = {
        token for token, count in eligible_by_guid.items() if count > 0
    }
    orphan_owners = eligible_owners - current
    return {
        "current_generator_count": len(current),
        "node_table_generator_count": len(eligible_owners),
        "orphan_generator_guid_count": len(orphan_owners),
        "orphan_node_count": sum(
            eligible_by_guid[token] for token in orphan_owners
        ),
        "eligible_node_count": sum(eligible_by_guid.values()),
        "total_node_count": before["total_node_count"],
        "stale": bool(orphan_owners),
    }


def anonymized_replay_document(before):
    """Build XML that preserves the public ownership relation only.

    The extra serialized Nodes represented by ``total_node_count`` are made
    hidden. They remain part of the total table size but are intentionally not
    added to any eligible owner's count.
    """
    evidence = before["guid_ownership_evidence"]
    current = evidence["current_generator_guid_tokens"]
    eligible_by_guid = evidence["eligible_node_count_by_guid_token"]
    generators = [
        (
            '<Generator Type="Leaf Mesh">'
            f"<Name>{token}</Name><GUID>{token}</GUID><Hidden>false</Hidden>"
            "<Properties></Properties></Generator>"
        )
        for token in current
    ]
    nodes = []
    index = 0
    for token, count in eligible_by_guid.items():
        for _ in range(count):
            nodes.append(
                "<Node>"
                f"<GeneratorGUID>{token}</GeneratorGUID>"
                "<ParentGUID></ParentGUID>"
                f"<Name>eligible-{index}</Name><GUID>node-{index}</GUID>"
                "<Hidden>false</Hidden>"
                "<Extra><m_bDeleted>false</m_bDeleted>"
                "<m_bCulled>false</m_bCulled></Extra>"
                "</Node>"
            )
            index += 1
    ineligible_count = before["total_node_count"] - sum(
        eligible_by_guid.values()
    )
    for _ in range(ineligible_count):
        nodes.append(
            "<Node>"
            f"<GeneratorGUID>{current[0]}</GeneratorGUID>"
            "<ParentGUID></ParentGUID>"
            f"<Name>ineligible-{index}</Name><GUID>node-{index}</GUID>"
            "<Hidden>true</Hidden>"
            "<Extra><m_bDeleted>false</m_bDeleted>"
            "<m_bCulled>false</m_bCulled></Extra>"
            "</Node>"
        )
        index += 1
    return (
        '<?xml version="1.0"?><SpeedTree>'
        f"<Generators>{''.join(generators)}</Generators>"
        f"<Nodes>{''.join(nodes)}</Nodes>"
        "</SpeedTree>"
    )


def elementtree_relationship(document_text):
    """Independently recompute the relation without audit regex helpers."""
    root = ET.fromstring(document_text)
    current = {
        (generator.findtext("GUID") or "").strip().casefold()
        for generator in root.iter("Generator")
    }
    current.discard("")
    eligible_by_guid = Counter()
    total = 0
    truthy = {"1", "true", "yes"}
    for node in root.iter("Node"):
        total += 1
        guid = (node.findtext("GeneratorGUID") or "").strip().casefold()
        hidden = (node.findtext("Hidden") or "").strip().casefold() in truthy
        deleted = (
            node.findtext("./Extra/m_bDeleted") or ""
        ).strip().casefold() in truthy
        culled = (
            node.findtext("./Extra/m_bCulled") or ""
        ).strip().casefold() in truthy
        if guid and not hidden and not deleted and not culled:
            eligible_by_guid[guid] += 1
    eligible_owners = set(eligible_by_guid)
    orphan_owners = eligible_owners - current
    return {
        "current_generator_guid_tokens": current,
        "eligible_node_count_by_guid_token": dict(eligible_by_guid),
        "current_generator_count": len(current),
        "node_table_generator_count": len(eligible_owners),
        "orphan_generator_guid_count": len(orphan_owners),
        "orphan_node_count": sum(
            eligible_by_guid[token] for token in orphan_owners
        ),
        "eligible_node_count": sum(eligible_by_guid.values()),
        "total_node_count": total,
        "stale": bool(orphan_owners),
    }


class FixtureContractTests(unittest.TestCase):
    def test_fixture_uses_v2_contract_and_names_the_incomplete_closure(self):
        data = load_fixture()
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(
            data["contract"], "speedtree_blackgum_node_table_evidence_v2"
        )
        self.assertEqual(data["issue_number"], 5)
        self.assertEqual(
            data["result_status"]["summary"],
            "pre-resave evidence valid / closure incomplete",
        )
        self.assertEqual(data["result_status"]["pre_resave_evidence"], "valid")
        self.assertEqual(data["result_status"]["closure"], "incomplete")
        self.assertEqual(
            data["audit_outcome"],
            "pre_resave_evidence_valid_closure_incomplete",
        )

    def test_audit_repository_commit_and_contract_are_pinned(self):
        provenance = load_fixture()["audit_repository"]
        self.assertRegex(provenance["repository_commit"], rf"^{SHA1_RE.pattern}$")
        self.assertEqual(len(provenance["repository_commit"]), 40)
        self.assertEqual(
            provenance["snapshot_contract"],
            "speedtree_live_generator_delivery_snapshot_v1",
        )
        self.assertEqual(
            provenance["independent_crosscheck_contract"],
            "python_xml_etree_elementtree_v1",
        )

    def test_public_fixture_has_no_absolute_path_username_or_raw_guid(self):
        raw = FIXTURE_PATH.read_text(encoding="utf-8")
        self.assertIsNone(WINDOWS_ABSOLUTE_PATH_RE.search(raw))
        self.assertIsNone(USER_HOME_PATH_RE.search(raw))
        self.assertIsNone(RAW_UUID_RE.search(raw))

    def test_asset_paths_are_relative_filenames_only(self):
        data = load_fixture()
        assets = data["stale_assets"] + data["coherent_reference_assets"]
        for asset in assets:
            path = Path(asset["asset_relative_path"])
            self.assertEqual(path.name, asset["asset_relative_path"])
            self.assertFalse(path.is_absolute())
            self.assertEqual(path.suffix, ".spm")


class BeforeEvidenceRelationshipTests(unittest.TestCase):
    def test_sha256_and_anonymized_guid_membership_are_complete(self):
        for asset in load_fixture()["stale_assets"]:
            before = asset["before"]
            self.assertRegex(before["spm_text_sha256"], rf"^{SHA256_RE.pattern}$")
            evidence = before["guid_ownership_evidence"]
            current = evidence["current_generator_guid_tokens"]
            eligible_by_guid = evidence["eligible_node_count_by_guid_token"]
            self.assertEqual(len(current), len(set(current)))
            self.assertEqual(len(current), before["current_generator_count"])
            self.assertTrue(set(current).issubset(eligible_by_guid))
            for token, count in eligible_by_guid.items():
                self.assertRegex(token, rf"^{GUID_TOKEN_RE.pattern}$")
                self.assertIs(type(count), int)
                self.assertGreaterEqual(count, 0)

    def test_current_owner_orphan_and_stale_are_recomputed(self):
        fields = (
            "current_generator_count",
            "node_table_generator_count",
            "orphan_generator_guid_count",
            "orphan_node_count",
            "eligible_node_count",
            "total_node_count",
            "stale",
        )
        for asset in load_fixture()["stale_assets"]:
            before = asset["before"]
            recomputed = relationship_from_anonymized_evidence(before)
            for field in fields:
                self.assertEqual(
                    recomputed[field],
                    before[field],
                    f"{asset['asset_id']}.before.{field}",
                )

    def test_total_node_count_is_not_conflated_with_eligible_owner_sum(self):
        for asset in load_fixture()["stale_assets"]:
            before = asset["before"]
            eligible_sum = sum(
                before["guid_ownership_evidence"]
                ["eligible_node_count_by_guid_token"].values()
            )
            self.assertEqual(eligible_sum, before["eligible_node_count"])
            self.assertGreater(before["total_node_count"], eligible_sum)
            self.assertEqual(before["total_node_count"] - eligible_sum, 1)

    def test_recorded_elementtree_crosscheck_matches_primary_evidence(self):
        fields = (
            "current_generator_count",
            "node_table_generator_count",
            "orphan_generator_guid_count",
            "orphan_node_count",
            "eligible_node_count",
            "total_node_count",
            "stale",
        )
        for asset in load_fixture()["stale_assets"]:
            before = asset["before"]
            crosscheck = before["independent_elementtree_crosscheck"]
            self.assertEqual(crosscheck["parser"], "xml.etree.ElementTree")
            self.assertEqual(crosscheck["status"], "matched")
            self.assertTrue(crosscheck["current_generator_guid_membership_match"])
            self.assertTrue(crosscheck["per_guid_eligible_node_counts_match"])
            for field in fields:
                self.assertEqual(crosscheck[field], before[field])


class IndependentParserReplayTests(unittest.TestCase):
    def test_elementtree_replays_membership_counts_and_relationship(self):
        fields = (
            "current_generator_count",
            "node_table_generator_count",
            "orphan_generator_guid_count",
            "orphan_node_count",
            "eligible_node_count",
            "total_node_count",
            "stale",
        )
        for asset in load_fixture()["stale_assets"]:
            before = asset["before"]
            evidence = before["guid_ownership_evidence"]
            result = elementtree_relationship(anonymized_replay_document(before))
            self.assertEqual(
                result["current_generator_guid_tokens"],
                set(evidence["current_generator_guid_tokens"]),
            )
            replay_counts = {
                token: result["eligible_node_count_by_guid_token"].get(token, 0)
                for token in evidence["eligible_node_count_by_guid_token"]
            }
            self.assertEqual(
                replay_counts, evidence["eligible_node_count_by_guid_token"]
            )
            for field in fields:
                self.assertEqual(result[field], before[field])

    def test_committed_regex_policy_replays_the_same_relation(self):
        for asset in load_fixture()["stale_assets"]:
            before = asset["before"]
            text = anonymized_replay_document(before)
            counts, total = audit._export_node_counts_from_text(text)
            state = audit._node_table_state(
                counts, total, audit._generator_guid_keys_from_text(text)
            )
            recomputed = relationship_from_anonymized_evidence(before)
            self.assertEqual(state["generator_count"], recomputed["current_generator_count"])
            self.assertEqual(
                state["node_table_generator_count"],
                recomputed["node_table_generator_count"],
            )
            self.assertEqual(
                len(state["orphan_generator_guids"]),
                recomputed["orphan_generator_guid_count"],
            )
            self.assertEqual(state["orphan_node_count"], recomputed["orphan_node_count"])
            self.assertEqual(state["total_node_count"], before["total_node_count"])
            self.assertEqual(sum(counts.values()), before["eligible_node_count"])
            self.assertEqual(state["stale"], before["stale"])


class TargetBindingAndDeliveryTests(unittest.TestCase):
    def test_target_bindings_are_current_zero_count_stale_evidence(self):
        data = load_fixture()
        expected_mesh_ids = set(data["delivery_context"]["declared_target_mesh_ids"])
        result = data["delivery_context"]["target_binding_result_before_resave"]
        self.assertTrue(result["all_required_mesh_ids_have_current_graph_bindings"])
        self.assertTrue(result["all_observed_bindings_graph_visible"])
        self.assertEqual(result["eligible_node_count_per_observed_binding"], 0)
        self.assertFalse(result["all_observed_bindings_export_participate"])
        self.assertEqual(result["export_evidence"], "node_table_stale")
        for asset in data["stale_assets"]:
            before = asset["before"]
            evidence = before["guid_ownership_evidence"]
            current = set(evidence["current_generator_guid_tokens"])
            eligible_by_guid = evidence["eligible_node_count_by_guid_token"]
            bindings = before["target_bindings"]
            self.assertEqual({row["mesh_id"] for row in bindings}, expected_mesh_ids)
            for row in bindings:
                self.assertIn(row["guid_token"], current)
                self.assertEqual(eligible_by_guid[row["guid_token"]], 0)

    def test_delivery_result_is_blocked_by_stale_evidence_not_disconnection(self):
        data = load_fixture()
        context = data["delivery_context"]
        result = context["delivery_result_before_resave"]
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"], "NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE")
        self.assertEqual(result["delivery_reason"], data["parser"]["stale_delivery_reason"])
        self.assertEqual(result["delivery_error"], data["parser"]["stale_delivery_error"])
        self.assertEqual(result["live_export_participating_target_mesh_ids"], [])
        self.assertEqual(result["downstream_effect"], "dependency_blocked")
        self.assertIsNone(context["delivery_result_after_resave"])


class PendingClosureTests(unittest.TestCase):
    def test_after_slots_remain_pending_and_unfabricated(self):
        null_fields = (
            "captured_utc",
            "spm_text_sha256",
            "current_generator_count",
            "node_table_generator_count",
            "orphan_generator_guid_count",
            "orphan_node_count",
            "eligible_node_count",
            "total_node_count",
            "stale",
            "guid_ownership_evidence",
            "independent_elementtree_crosscheck",
            "target_bindings",
            "delivery_result",
        )
        for asset in load_fixture()["stale_assets"]:
            after = asset["after"]
            self.assertEqual(after["status"], "pending")
            self.assertFalse(after["resaved"])
            for field in null_fields:
                self.assertIsNone(after[field], f"{asset['asset_id']}.after.{field}")
            self.assertEqual(
                asset["audit_outcome"],
                "pre_resave_evidence_valid_closure_incomplete",
            )

    def test_resave_and_acceptance_items_remain_open(self):
        data = load_fixture()
        self.assertFalse(data["resave_tool"]["executed"])
        self.assertIsNone(data["resave_tool"]["executed_utc"])
        self.assertIsNone(data["resave_tool"]["operator"])
        pending = {
            row["item"]
            for row in data["completion_checklist"]
            if row["status"] == "pending"
        }
        self.assertEqual(len(pending), 4)


if __name__ == "__main__":
    unittest.main()
