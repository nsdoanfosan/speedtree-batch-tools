"""Literal compatibility-matrix and sanitized real-evidence tests for #41."""

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import stale_node_table_recovery as recovery  # noqa: E402
from stale_node_table_recovery import (  # noqa: E402
    StaleNodeTableRecoveryError,
    _resolve_receipt_dialect,
    recover_stale_node_table,
    verify_sealed_resave,
)


FIXTURE = Path(__file__).parent / "fixtures" / (
    "issue_41_receipt_compatibility.json"
)
LITERAL_ROOT = Path(__file__).parent / "fixtures" / "issue_41"
LITERAL_CASES = (
    "schema2",
    "schema3_core1_unsupported",
    "schema3_core2",
    "schema4",
    "schema5",
)
FIXED_IDENTITY = {
    "asset_name": "model.spm",
    "source_identity_sha256": "a" * 64,
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:(?<![a-z])[a-z]:[\\/]|users[\\/][^\\/]+)"
)
RAW_GUID_RE = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def receipt_for(case):
    versions = case["projections"]
    receipt = {
        "schema_version": case["receipt_schema_version"],
        "authoring_graph_projection": {
            "contract": "speedtree_spm_authoring_graph_projection",
            "version": versions["authoring_graph"],
        },
        "generator_membership": {
            "contract": "speedtree_generator_membership_projection",
            "version": versions["generator_membership"],
        },
        "required_target_bindings": {
            "contract": "speedtree_required_target_binding_projection",
            "version": versions["required_target_bindings"],
        },
    }
    if versions["authoring_graph_core"] is not None:
        receipt["authoring_graph_core_projection"] = {
            "contract": "speedtree_spm_authoring_graph_core_projection",
            "version": versions["authoring_graph_core"],
        }
    if versions["target_requirements"] is not None:
        receipt["target_requirements"] = {
            "contract": "speedtree_stale_node_target_requirements",
            "version": versions["target_requirements"],
        }
    return receipt


class ReceiptCompatibilityFixtureTests(unittest.TestCase):
    def test_fixture_is_sanitized_and_issue_scoped(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["issue_number"], 41)
        self.assertIsNone(WINDOWS_PATH_RE.search(raw))
        self.assertIsNone(RAW_GUID_RE.search(raw))

    def test_literal_matrix_matches_the_runtime_registry(self):
        for case in load_fixture()["compatibility_matrix"]:
            with self.subTest(case=case["name"]):
                receipt = receipt_for(case)
                if case["result"] == "supported":
                    self.assertEqual(
                        _resolve_receipt_dialect(receipt),
                        case["name"],
                    )
                else:
                    with self.assertRaises(
                        StaleNodeTableRecoveryError
                    ) as caught:
                        _resolve_receipt_dialect(receipt)
                    self.assertEqual(
                        caught.exception.reason_token,
                        case["reason_token"],
                    )

    def test_real_core_v1_probe_is_explicit_and_byte_preserving(self):
        probe = load_fixture()["sanitized_real_legacy_probe"]
        self.assertEqual(
            probe["projection_versions"],
            [3, 1, 1, 1, 1, None],
        )
        self.assertEqual(
            probe["reason_token"],
            "preimage_receipt_projection_version_unsupported",
        )
        self.assertTrue(probe["receipt_bytes_unchanged"])
        self.assertEqual(
            probe["receipt_sha256_before"],
            probe["receipt_sha256_after"],
        )
        for field in (
            "receipt_sha256_before",
            "receipt_sha256_after",
            "backup_raw_sha256",
            "authoring_graph_fingerprint",
            "authoring_graph_core_fingerprint",
            "generator_membership_fingerprint",
            "required_target_binding_fingerprint",
        ):
            self.assertRegex(probe[field], SHA256_RE)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class LiteralHistoricalReceiptTests(unittest.TestCase):
    def fixture(self, name):
        root = LITERAL_ROOT / name
        receipt_bytes = (root / "receipt.json").read_bytes()
        return {
            "root": root,
            "backup_bytes": (root / "backup.spm").read_bytes(),
            "after_bytes": (root / "after.spm").read_bytes(),
            "receipt_bytes": receipt_bytes,
            "receipt": json.loads(receipt_bytes.decode("utf-8")),
            "expected": json.loads(
                (root / "expected.json").read_text(encoding="utf-8")
            ),
        }

    def scopes(self, receipt):
        if receipt["schema_version"] == 5:
            return {
                "expected_mesh_ids": (),
                "authoring_mesh_ids": (130,),
                "required_live_mesh_ids": (130,),
            }
        return {
            "expected_mesh_ids": (130,),
            "authoring_mesh_ids": None,
            "required_live_mesh_ids": None,
        }

    def materialize(self, fixture, folder, *, saved):
        receipt = fixture["receipt"]
        model = folder / "model.spm"
        model.write_bytes(
            fixture["after_bytes"] if saved else fixture["backup_bytes"]
        )
        root = folder / "recovery"
        root.mkdir()
        backup = root / receipt["exact_preimage"]["backup_file"]
        backup.write_bytes(fixture["backup_bytes"])
        receipt_path = root / (
            "model."
            + receipt["exact_preimage"]["raw_sha256"]
            + ".receipt.json"
        )
        receipt_path.write_bytes(fixture["receipt_bytes"])
        return model, root, backup, receipt_path

    def test_literal_bytes_are_complete_sanitized_and_hash_bound(self):
        for name in LITERAL_CASES:
            with self.subTest(case=name):
                fixture = self.fixture(name)
                raw = fixture["receipt_bytes"].decode("utf-8")
                receipt = fixture["receipt"]
                self.assertIsNone(WINDOWS_PATH_RE.search(raw))
                self.assertIsNone(RAW_GUID_RE.search(raw))
                self.assertEqual(receipt["asset_name"], "model.spm")
                self.assertEqual(
                    hashlib.sha256(fixture["backup_bytes"]).hexdigest(),
                    receipt["exact_preimage"]["raw_sha256"],
                )
                self.assertEqual(
                    len(fixture["backup_bytes"]),
                    receipt["exact_preimage"]["size"],
                )
        schema2_raw = self.fixture("schema2")["receipt_bytes"]
        self.assertTrue(schema2_raw.endswith(b"\n"))
        self.assertIn(b'    "recovery_contract"', schema2_raw)

    def test_literal_supported_receipts_verify_and_restart_byte_for_byte(self):
        for name in LITERAL_CASES:
            fixture = self.fixture(name)
            if fixture["expected"]["result"] != "supported":
                continue
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                model, _root, backup, receipt_path = self.materialize(
                    fixture,
                    folder,
                    saved=True,
                )
                scopes = self.scopes(fixture["receipt"])
                receipt_sha = hashlib.sha256(
                    fixture["receipt_bytes"]
                ).hexdigest()
                with mock.patch.object(
                    recovery,
                    "_source_identity",
                    return_value=dict(FIXED_IDENTITY),
                ):
                    result = verify_sealed_resave(
                        model,
                        backup,
                        receipt_path,
                        scopes["expected_mesh_ids"],
                        authoring_mesh_ids=scopes["authoring_mesh_ids"],
                        required_live_mesh_ids=scopes[
                            "required_live_mesh_ids"
                        ],
                    )
                self.assertEqual(
                    result["status"], fixture["expected"]["verify_status"]
                )
                self.assertFalse(result["modeler_launched"])
                self.assertEqual(
                    result["preimage_receipt_sha256"], receipt_sha
                )
                self.assertEqual(receipt_path.read_bytes(), fixture["receipt_bytes"])
                self.assertEqual(backup.read_bytes(), fixture["backup_bytes"])

            with self.subTest(case=name, path="restart"), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                model, root, backup, receipt_path = self.materialize(
                    fixture,
                    folder,
                    saved=False,
                )
                executable = folder / "SpeedTree_Modeler.exe"
                executable.write_bytes(b"fixture executable")
                launches = []
                clock = _Clock()

                def launch(_exe, _spm):
                    launches.append(True)
                    model.write_bytes(fixture["after_bytes"])
                    return object()

                scopes = self.scopes(fixture["receipt"])
                with mock.patch.object(
                    recovery,
                    "_source_identity",
                    return_value=dict(FIXED_IDENTITY),
                ):
                    result = recover_stale_node_table(
                        model,
                        executable,
                        scopes["expected_mesh_ids"],
                        authoring_mesh_ids=scopes["authoring_mesh_ids"],
                        required_live_mesh_ids=scopes[
                            "required_live_mesh_ids"
                        ],
                        timeout=10,
                        poll_interval=1,
                        stable_reads=2,
                        recovery_root=root,
                        launch_fn=launch,
                        sleep_fn=clock.sleep,
                        monotonic_fn=clock.monotonic,
                    )
                self.assertEqual(
                    result["status"], fixture["expected"]["restart_status"]
                )
                self.assertEqual(len(launches), 1)
                self.assertFalse(result["retry_invoked"])
                self.assertIsNone(result["continuation_claim"])
                self.assertEqual(receipt_path.read_bytes(), fixture["receipt_bytes"])
                self.assertEqual(backup.read_bytes(), fixture["backup_bytes"])
                self.assertFalse(list(root.glob("continuation.*.claim.json")))

    def test_literal_core1_receipt_fails_before_launch_without_rewrite(self):
        fixture = self.fixture("schema3_core1_unsupported")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            model, root, backup, receipt_path = self.materialize(
                fixture,
                folder,
                saved=False,
            )
            executable = folder / "SpeedTree_Modeler.exe"
            executable.write_bytes(b"fixture executable")
            launches = []
            with mock.patch.object(
                recovery,
                "_source_identity",
                return_value=dict(FIXED_IDENTITY),
            ), self.assertRaises(StaleNodeTableRecoveryError) as verify_error:
                verify_sealed_resave(
                    model,
                    backup,
                    receipt_path,
                    (130,),
                )
            self.assertEqual(
                verify_error.exception.reason_token,
                fixture["expected"]["reason_token"],
            )
            with mock.patch.object(
                recovery,
                "_source_identity",
                return_value=dict(FIXED_IDENTITY),
            ), self.assertRaises(StaleNodeTableRecoveryError) as caught:
                recover_stale_node_table(
                    model,
                    executable,
                    (130,),
                    recovery_root=root,
                    launch_fn=lambda *_args: launches.append(True),
                )
            self.assertEqual(
                caught.exception.reason_token,
                fixture["expected"]["reason_token"],
            )
            self.assertFalse(launches)
            self.assertEqual(receipt_path.read_bytes(), fixture["receipt_bytes"])
            self.assertEqual(backup.read_bytes(), fixture["backup_bytes"])

    def test_target_v1_count_cannot_mix_with_another_candidate(self):
        fixture = self.fixture("schema2")
        tampered = json.loads(fixture["receipt_bytes"].decode("utf-8"))
        tampered["required_target_bindings"]["binding_count"] = 2
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            model, _root, backup, receipt_path = self.materialize(
                fixture,
                folder,
                saved=True,
            )
            receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
            with mock.patch.object(
                recovery,
                "_source_identity",
                return_value=dict(FIXED_IDENTITY),
            ), self.assertRaises(StaleNodeTableRecoveryError) as caught:
                verify_sealed_resave(
                    model,
                    backup,
                    receipt_path,
                    (130,),
                )
            self.assertEqual(
                caught.exception.reason_token,
                "preimage_receipt_verification_failed",
            )

    def test_semantic_registry_is_immutable_and_owns_projector_policy(self):
        with self.assertRaises(TypeError):
            recovery._RECEIPT_DIALECTS[(99,)] = object()
        for versions, dialect in recovery._RECEIPT_DIALECTS.items():
            with self.subTest(versions=versions):
                self.assertEqual(dialect.versions, versions)
                policies = [
                    dialect.graph,
                    dialect.membership,
                    dialect.targets,
                ]
                if dialect.core is not None:
                    policies.append(dialect.core)
                for policy in policies:
                    self.assertTrue(callable(policy.candidate_projector))
                    self.assertTrue(policy.authoritative_fields)
        target_v1 = recovery._RECEIPT_DIALECTS[
            (2, 1, None, 1, 1, None)
        ].targets
        self.assertEqual(
            target_v1.authoritative_fields,
            ("fingerprint", "binding_count", "expected_mesh_ids"),
        )

    def test_current_writer_uses_explicit_dialect_not_mutable_constants(self):
        fixture = self.fixture("schema5")
        snapshot = recovery._capture_immutable_snapshot(
            fixture["root"] / "backup.spm",
            (130,),
        )
        scopes = {
            "authoring_mesh_ids": [130],
            "required_live_mesh_ids": [130],
        }
        with mock.patch.object(
            recovery,
            "AUTHORING_GRAPH_CORE_PROJECTION_VERSION",
            99,
        ), mock.patch.object(
            recovery,
            "TARGET_BINDING_PROJECTION_VERSION",
            99,
        ), mock.patch.object(
            recovery,
            "TARGET_REQUIREMENTS_VERSION",
            99,
        ):
            receipt = recovery._preimage_receipt(
                snapshot,
                scopes,
                "literal.preimage.spm",
            )
        self.assertEqual(
            recovery._receipt_dialect_versions(receipt),
            (6, 1, 4, 1, 2, 1),
        )


if __name__ == "__main__":
    unittest.main()
