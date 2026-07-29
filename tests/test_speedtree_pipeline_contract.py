import gzip
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PCG_DIR = REPO / "pcg_st9_texture_batch"
SK_DIR = REPO / "sk_batch"
for path in (REPO, PCG_DIR, SK_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from pcg_texture_audit import canonical_material_name, derived_material_base
from spm_audit import read_spm as read_sk_spm, write_spm as write_sk_spm
from speedtree_pipeline_contract import (
    BACKUP_DIRECTORY_NAMES,
    build_preflight_envelope,
    canonical_path,
    is_live_spm,
    naming_shadow_issue,
    prove_legacy_texture_normalize_semantic_migration,
    read_spm_text,
    read_tree_instance_profile,
    shared_contract_api,
    source_set_fingerprint,
    speedtree_stmat_path,
    spm_container_format,
    spm_file_structural_semantic_fingerprint,
    validate_preflight_envelope,
)
from speedtree_texture_contract import REQUIRED_TEXTURE_ROLES, resolve_texture_bindings


def spm_xml(profile=""):
    return (
        "<SpeedTreeModel><Generators><Generator Type=\"Tree\">"
        "<Name>Tree</Name><Properties><Property>"
        "<Name>SpeedTree SDK:User data</Name>"
        f"<Value>{profile}</Value>"
        "</Property></Properties></Generator></Generators></SpeedTreeModel>"
    )


def write_spm(path, profile="", compressed=True):
    payload = spm_xml(profile).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(payload) if compressed else payload)
    return path


def write_stmat(spm, material_names, texture_base="T_Leaf_grass_Atlas_01"):
    stmat = speedtree_stmat_path(spm)
    texture_dir = spm.parent / "texture"
    texture_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for role in REQUIRED_TEXTURE_ROLES:
        path = texture_dir / f"{texture_base}_{role}.tga"
        path.write_bytes(role.encode("ascii"))
        files[role] = path
    materials = []
    for index, material_name in enumerate(material_names, start=10):
        maps = "".join(
            f'<Map Name="{role}" Source="{files[role]}" />'
            for role in REQUIRED_TEXTURE_ROLES
        )
        materials.append(
            f'<Material ID="{index}" Name="{material_name}">{maps}</Material>'
        )
    stmat.parent.mkdir(parents=True, exist_ok=True)
    stmat.write_text(
        "<SpeedTreeMaterials>" + "".join(materials) + "</SpeedTreeMaterials>",
        encoding="utf-8",
    )
    return stmat


class SpeedTreePipelineContractTests(unittest.TestCase):
    def test_structural_spm_fingerprint_ignores_shading_but_not_geometry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "SK_leaf.spm"

            def structural_xml(texture, material_name, cutout_mesh_id):
                return (
                    "<SpeedTreeModel>"
                    "<Materials>"
                    f'<Material_v8 ID=\"4\" Name=\"{material_name}\">'
                    f"<TexFilename>{texture}</TexFilename>"
                    f"<CutoutMeshID>{cutout_mesh_id}</CutoutMeshID>"
                    "<SupplementalCutoutMeshIDs Count=\"0\" />"
                    "<UVAreas Count=\"1\"><UVArea>0 0 1 1</UVArea></UVAreas>"
                    "</Material_v8>"
                    "</Materials>"
                    "<Generators><Generator Type=\"Leaf\">"
                    "<Name>Leaf</Name><Properties><Property>"
                    "<Name>SpeedTree SDK:Material:Frond</Name><Value>4</Value>"
                    "</Property></Properties></Generator></Generators>"
                    "</SpeedTreeModel>"
                )

            source.write_bytes(
                gzip.compress(
                    structural_xml("old.tif", "OldMaterial", 7).encode("utf-8")
                )
            )
            original = spm_file_structural_semantic_fingerprint(source)
            source.write_bytes(
                gzip.compress(
                    structural_xml("new.tga", "NewMaterial", 7).encode("utf-8")
                )
            )
            self.assertEqual(
                spm_file_structural_semantic_fingerprint(source),
                original,
            )
            source.write_bytes(
                gzip.compress(
                    structural_xml("new.tga", "NewMaterial", 8).encode("utf-8")
                )
            )
            self.assertNotEqual(
                spm_file_structural_semantic_fingerprint(source),
                original,
            )

    def test_legacy_semantic_migration_requires_exact_normalize_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Cluster" / "SK_leaf.spm"
            source.parent.mkdir(parents=True)
            before = (
                "<SpeedTreeModel><Materials>"
                '<Material_v8 ID="4" Name="Old">'
                "<TexFilename>old.tif</TexFilename><CutoutMeshID>7</CutoutMeshID>"
                '<SupplementalCutoutMeshIDs Count="0" />'
                '<UVAreas Count="1"><UVArea>0 0 1 1</UVArea></UVAreas>'
                "</Material_v8></Materials></SpeedTreeModel>"
            )
            after = before.replace("Old", "New").replace("old.tif", "new.tga")
            source.write_bytes(gzip.compress(before.encode("utf-8")))
            recorded_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            backup_dir = (
                source.parent
                / "reports"
                / "texture_normalize_backups"
                / "texture_normalize_20260729_030505"
            )
            backup_dir.mkdir(parents=True)
            backup = backup_dir / f"0001_{source.name}"
            backup.write_bytes(source.read_bytes())
            source.write_bytes(gzip.compress(after.encode("utf-8")))

            self.assertIsNone(
                prove_legacy_texture_normalize_semantic_migration(
                    source,
                    recorded_hash,
                )
            )
            receipt = source.parent / "reports" / "normalize.json"
            (source.parent / "reports" / "000_spm_audit.json").write_text(
                json.dumps([{"status": "already-ok", "spm": str(source)}]),
                encoding="utf-8",
            )
            receipt.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "normalization": {
                            "backup_dir": str(backup_dir),
                            "spms": [str(source)],
                        },
                    }
                ),
                encoding="utf-8",
            )
            evidence = prove_legacy_texture_normalize_semantic_migration(
                source,
                recorded_hash,
            )
            self.assertEqual(
                evidence["status"],
                "legacy_texture_normalize_migrated",
            )
            self.assertTrue(evidence["raw_sha256_drift"])

            structural_change = after.replace(
                "<CutoutMeshID>7</CutoutMeshID>",
                "<CutoutMeshID>8</CutoutMeshID>",
            )
            source.write_bytes(gzip.compress(structural_change.encode("utf-8")))
            self.assertIsNone(
                prove_legacy_texture_normalize_semantic_migration(
                    source,
                    recorded_hash,
                )
            )

    def test_plain_and_gzip_spm_use_the_same_tree_profile_rule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain = write_spm(root / "SK_plain.spm", "Dead", compressed=False)
            compressed = write_spm(root / "SK_gzip.spm", "Dead", compressed=True)

            self.assertEqual(spm_container_format(plain), "plain_xml")
            self.assertEqual(spm_container_format(compressed), "gzip")
            self.assertEqual(read_spm_text(plain), read_spm_text(compressed))
            self.assertEqual(read_sk_spm(plain), read_sk_spm(compressed))
            self.assertEqual(read_tree_instance_profile(plain), "dead")
            self.assertEqual(read_tree_instance_profile(compressed), "dead")
            write_sk_spm(plain, read_sk_spm(plain))
            self.assertEqual(spm_container_format(plain), "plain_xml")

    def test_tree_profile_and_production_group_are_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = write_spm(root / "SK_grass.spm", "")
            stmat = write_stmat(
                spm,
                [
                    "M_Leaf_common_grass_01_dead_Mat",
                    "M_Leaf_common_grass_01_green_Mat",
                ],
            )
            readiness = resolve_texture_bindings(stmat)
            envelope = build_preflight_envelope(
                spm,
                outcome="ok",
                texture_readiness=readiness,
            )

            self.assertEqual(envelope["instance_profile"], "")
            self.assertEqual(len(envelope["material_intents"]), 2)
            by_name = {
                item["material_name"]: item
                for item in envelope["material_intents"]
            }
            self.assertEqual(
                by_name["M_Leaf_common_grass_01_dead_Mat"]["production_group_tokens"],
                ["dead"],
            )
            self.assertNotIn(
                "profile_target_name",
                by_name["M_Leaf_common_grass_01_dead_Mat"],
            )

    def test_dead_and_green_share_the_authoritative_stmat_texture_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = write_spm(root / "SK_Weed_Common_grass_c_01.spm")
            stmat = write_stmat(
                spm,
                [
                    "M_Leaf_common_grass_01_dead_Mat",
                    "M_Leaf_common_grass_01_green_Mat",
                ],
                texture_base="T_Leaf_grass_Atlas_01",
            )
            readiness = resolve_texture_bindings(stmat)
            envelope = build_preflight_envelope(
                spm,
                outcome="ok",
                texture_readiness=readiness,
            )

            bindings = [
                item["texture_binding"]
                for item in envelope["material_intents"]
            ]
            self.assertEqual(
                [item["texture_base"] for item in bindings],
                ["T_Leaf_grass_Atlas_01", "T_Leaf_grass_Atlas_01"],
            )
            self.assertTrue(all(item["status"] == "ok" for item in bindings))
            self.assertTrue(
                all(
                    item["texture_source_mode"] == "managed_texture_set"
                    for item in envelope["material_intents"]
                )
            )

    def test_tree_profile_is_applied_once_to_every_stmat_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = write_spm(root / "SK_stem.spm", "Dead")
            stmat = write_stmat(spm, ["M_stem_common_01_Mat"])
            envelope = build_preflight_envelope(
                spm,
                outcome="ok",
                texture_readiness=resolve_texture_bindings(stmat),
            )

            intent = envelope["material_intents"][0]
            self.assertEqual(envelope["instance_profile"], "dead")
            self.assertEqual(intent["instance_profile"], "dead")
            self.assertEqual(intent["profile_target_name"], "MI_stem_common_01_dead")

    def test_invalid_tree_user_data_is_not_replaced_by_a_material_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = write_spm(root / "SK_invalid_profile.spm", "../dead")
            stmat = write_stmat(spm, ["M_leaf_grass_dead_Mat"])

            with self.assertRaises(ValueError):
                read_tree_instance_profile(spm)
            envelope = build_preflight_envelope(
                spm,
                outcome="blocked",
                texture_readiness=resolve_texture_bindings(stmat),
            )
            self.assertEqual(envelope["instance_profile"], "")
            self.assertEqual(envelope["material_intents"], [])
            self.assertIn(
                "INSTANCE_PROFILE_INVALID",
                {issue["code"] for issue in envelope["issues"]},
            )

    def test_provenance_rejects_another_spm_and_changed_stmat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = write_spm(root / "SK_first.spm")
            second = write_spm(root / "SK_second.spm")
            first_stmat = write_stmat(first, ["M_leaf_01_Mat"])
            write_stmat(second, ["M_leaf_01_Mat"])
            envelope = build_preflight_envelope(
                first,
                outcome="ok",
                texture_readiness=resolve_texture_bindings(first_stmat),
            )

            with self.assertRaisesRegex(ValueError, "another model"):
                validate_preflight_envelope(envelope, second)
            first_stmat.write_text("<SpeedTreeMaterials />", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "STMAT source is stale"):
                validate_preflight_envelope(envelope, first)

    def test_source_fingerprint_is_canonical_content_identity_sha256(self):
        source = {
            "spm": {
                "canonical_path": r"C:\Trees\SK_tree.spm",
                "sha256": "a" * 64,
                "size": 10,
                "mtime_ns": 20,
            },
            "stmat": [],
        }
        encoded = json.dumps(
            {
                "spm": {
                    "canonical_path": r"C:\Trees\SK_tree.spm",
                    "sha256": "a" * 64,
                    "size": 10,
                },
                "stmat": [],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        self.assertEqual(
            source_set_fingerprint(source), hashlib.sha256(encoded).hexdigest()
        )
        touched = json.loads(json.dumps(source))
        touched["spm"]["mtime_ns"] = 999
        self.assertEqual(
            source_set_fingerprint(touched),
            source_set_fingerprint(source),
        )

    def test_preflight_validation_allows_touch_only_source_metadata_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = write_spm(root / "SK_tree.spm")
            stmat = write_stmat(spm, ["M_leaf_01_Mat"])
            envelope = build_preflight_envelope(
                spm,
                outcome="ok",
                texture_readiness=resolve_texture_bindings(stmat),
            )

            for source in (spm, stmat):
                stat = source.stat()
                os.utime(
                    source,
                    ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000),
                )

            validate_preflight_envelope(envelope, spm)

    def test_preflight_validation_rejects_unknown_fingerprint_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = write_spm(root / "SK_tree.spm")
            stmat = write_stmat(spm, ["M_leaf_01_Mat"])
            envelope = build_preflight_envelope(
                spm,
                outcome="ok",
                texture_readiness=resolve_texture_bindings(stmat),
            )
            envelope["source_fingerprint_policy"] = "future_unknown_policy"

            with self.assertRaisesRegex(ValueError, "unsupported"):
                validate_preflight_envelope(envelope, spm)

    def test_all_backup_namespaces_are_not_live_spms(self):
        self.assertIn(
            "_atlas_cluster_normalization_backups",
            BACKUP_DIRECTORY_NAMES,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = write_spm(root / "SK_live.spm")
            self.assertTrue(is_live_spm(live))
            for directory in BACKUP_DIRECTORY_NAMES:
                backup = write_spm(root / directory / "SK_backup.spm")
                with self.subTest(directory=directory):
                    self.assertFalse(is_live_spm(backup))

    def test_pcg_production_group_uses_numeric_suffix_not_token_allowlist(self):
        self.assertEqual(
            derived_material_base(
                "M_Leaf_common_grass_01_UserDefinedWinter"
            ),
            "M_Leaf_common_grass_01",
        )
        self.assertIsNone(derived_material_base("M_stem_common_01"))

    def test_golden_vectors_are_consumed_from_the_central_api(self):
        api = shared_contract_api()
        vectors = api.golden_vectors()
        for row in vectors["tree_axes"]:
            with self.subTest(material=row["name"]):
                intent = api.build_material_intent(row["name"])
                self.assertEqual(intent["tree_part"], row["tree_part"])
                self.assertEqual(intent["tree_shading"], row["tree_shading"])

    def test_pcg_and_sk_naming_difference_is_a_shadow_issue_only(self):
        name = "M_bark_common_locast_end_01"
        sk_name = name if name.casefold().startswith("m_") else "M_" + name
        issue = naming_shadow_issue(
            name,
            production_canonical_name=canonical_material_name(name),
            workflow_canonical_name=sk_name,
            workflow="sk_batch",
        )

        self.assertEqual(issue["code"], "MATERIAL_CANONICAL_NAME_DIVERGENCE")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["details"]["workflow_canonical_name"], name)
        self.assertEqual(
            issue["details"]["production_canonical_name"],
            "M_bark_common_end_01",
        )

    def test_dynamic_wind_path_uses_the_central_rule(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = write_spm(Path(temporary) / "SK_tree.spm")
            envelope = build_preflight_envelope(spm, outcome="blocked")
            suffix = shared_contract_api().dynamic_wind_rules()["filename_suffix"]
            self.assertEqual(
                Path(envelope["dynamic_wind"]["path"]).name,
                spm.stem + suffix,
            )
            self.assertEqual(
                envelope["source"]["spm"]["canonical_path"], canonical_path(spm)
            )


if __name__ == "__main__":
    unittest.main()
