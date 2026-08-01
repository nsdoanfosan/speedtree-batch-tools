import gzip
import hashlib
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PCG = REPO / "pcg_st9_texture_batch"
SHARED_FIXTURE = (
    PCG / "tests" / "fixtures" / "preview_role_fallback_v1.json"
)
sys.path.insert(0, str(PCG))
sys.path.insert(0, str(REPO))

from sk_batch.jobs import speedtree_material_preflight as preflight
from pcg_st9_texture_batch import pcg_cluster_assembly_contract as assembly
from pcg_texture_audit import (
    cluster_render_origin_receipt,
    preserved_cluster_materials,
    read_maybe_gzip_text,
)
from spm_texture_normalize import (
    build_spm_patch,
    inspect_material_slots,
    jobs_from_texture_plan,
)
from speedtree_texture_contract import (
    BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW,
    BLENDER_BAKE_PREVIEW_FALLBACK_CAPABILITY,
    inspect_spm_texture_slots,
    resolve_blender_cluster_bake_origin,
    validate_blender_cluster_bake_receipt_for_consumption,
)
from speedtree_preview_texture_contract import (
    FALLBACK_CANONICAL_FIELDS,
    PREVIEW_FALLBACK_CAPABILITY,
    PREVIEW_ONLY_USAGE,
    finalize_preview_receipt,
    validate_preview_receipt,
)


class Issue82PreviewSubsurfaceFallbackTests(unittest.TestCase):
    def test_shared_cross_reader_fixture_digest_and_order(self):
        receipt = json.loads(SHARED_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(
            BLENDER_BAKE_PREVIEW_FALLBACK_CAPABILITY,
            PREVIEW_FALLBACK_CAPABILITY,
        )
        validate_preview_receipt(
            receipt,
            requested_usage=PREVIEW_ONLY_USAGE,
        )
        self.assertEqual(
            tuple(receipt["preview_role_fallbacks"][0]),
            FALLBACK_CANONICAL_FIELDS,
        )
        self.assertEqual(
            receipt["receipt_core_sha256"],
            "475084cc428b608b45f6124195d7fe85adf655acc2511109932678e9fd9b7fed",
        )
        self.assertEqual(finalize_preview_receipt(receipt), receipt)

    def make_fixture(
        self,
        temporary,
        *,
        amount_selection="subsurface_color",
        normal_selection="normal",
    ):
        root = Path(temporary) / "tree_Lauraceae"
        cluster = root / "cluster"
        cluster.mkdir(parents=True)
        base = "cluster_Lauraceae_01"
        paths = {
            "Color": cluster / f"{base}.tga",
            "Opacity": cluster / f"{base}_Opacity.tga",
            "Normal": cluster / f"{base}_Normal.tga",
            "Gloss": cluster / f"{base}_Gloss.tga",
            "SubsurfaceColor": cluster / f"{base}_Subsurface.tga",
            "SubsurfaceAmount": cluster / f"{base}_SubsurfaceAmount.tga",
            "AO": cluster / f"{base}_AO.tga",
            "Height": cluster / f"{base}_Height.tga",
        }
        for role, path in paths.items():
            path.write_bytes(f"sanitized-{role}".encode("ascii"))
        unowned = cluster / f"{base}_Unowned.tga"
        unowned.write_bytes(b"sanitized-unowned")
        outside = root / f"{base}_Outside.tga"
        outside.write_bytes(b"sanitized-outside")

        contract_sha256 = hashlib.sha256(
            b"sanitized-lauraceae-physical-capture"
        ).hexdigest()
        manifest = cluster / f"{base}_auto_capture_manifest.json"
        manifest.write_text(json.dumps({
            "kind": "speedtree_cluster_blender_auto_capture",
            "version": 2,
            "workflow_mode": "PHYSICAL_DIRECT_CAPTURE",
            "direct_uv_source":
                "same_blender_physical_capture_projection",
            "physical_capture_contract_sha256": contract_sha256,
            "maps": [
                {
                    "role": role,
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "physical_capture_contract_sha256": contract_sha256,
                }
                for role, path in paths.items()
            ],
        }), encoding="utf-8")

        selected_amount = {
            "subsurface_color": paths["SubsurfaceColor"],
            "unowned": unowned,
            "outside": outside,
            "exact": paths["SubsurfaceAmount"],
        }[amount_selection]
        selected_normal = {
            "normal": paths["Normal"],
            "color": paths["Color"],
        }[normal_selection]
        target_paths = {
            **paths,
            "Normal": selected_normal,
            "SubsurfaceAmount": selected_amount,
        }

        def write_spm(path, material_name, map_paths):
            def authored_ref(map_path):
                if map_path.parent == cluster:
                    return f"cluster/{map_path.name}"
                return str(map_path.resolve())

            map_rows = []
            for map_name, map_path in map_paths.items():
                map_rows.append(
                    f'<Map Name="{map_name}"><TexFilename>'
                    f'{authored_ref(map_path)}</TexFilename></Map>'
                )
                if map_name == "Gloss":
                    map_rows.extend((
                        '<Map Name="Specular"><TexFilename /></Map>',
                        '<Map Name="Metallic"><TexFilename /></Map>',
                    ))
            maps = "".join(map_rows)
            text = (
                '<SpeedTree><Materials><Material_v8 ID="2" '
                f'Name="{material_name}">{maps}</Material_v8></Materials>'
                '<Generator><Property><Name>Leaves:Type:0:Material</Name>'
                '<Value>2</Value></Property></Generator></SpeedTree>'
            )
            path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))

        source = root / "tree_Lauraceae_10.spm"
        target = root / "SK_tree_Lauraceae_10.spm"
        write_spm(source, "cluster_lauraceae_01", paths)
        write_spm(target, "M_cluster_lauraceae_01", target_paths)

        stmat = root / "fbx" / "SK_tree_Lauraceae_10.stmat"
        stmat.parent.mkdir()
        stmat_root = ET.Element("SpeedTreeMaterials")
        material = ET.SubElement(
            stmat_root,
            "Material",
            ID="2",
            Name="M_cluster_lauraceae_01_Mat",
        )
        for map_name, map_path in target_paths.items():
            ET.SubElement(
                material,
                "Map",
                Name=map_name,
                Source=str(map_path.resolve()),
            )
            if map_name == "Gloss":
                ET.SubElement(
                    material,
                    "Map",
                    Name="Specular",
                    Source="",
                )
        ET.ElementTree(stmat_root).write(
            stmat,
            encoding="utf-8",
            xml_declaration=True,
        )
        return {
            "root": root,
            "cluster": cluster,
            "source": source,
            "target": target,
            "stmat": stmat,
            "manifest": manifest,
            "paths": paths,
            "target_paths": target_paths,
            "contract_sha256": contract_sha256,
        }

    @staticmethod
    def resolver_inputs(fixture):
        inspection = inspect_spm_texture_slots(fixture["target"])
        material = inspection["materials"][0]
        output = {
            "slot_files": [
                {
                    "map_index": slot["map_index"],
                    "map": slot["map"],
                    "role": slot["role"],
                    "path": slot["resolved_ref"],
                }
                for slot in material["slots"]
            ],
        }
        return material, output

    def test_preflight_accepts_fallback_while_step3_can_optionally_repair_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(temporary)
            before_bytes = fixture["target"].read_bytes()
            before_mtime_ns = fixture["target"].stat().st_mtime_ns

            material, output = self.resolver_inputs(fixture)
            producer_receipt, issue = resolve_blender_cluster_bake_origin(
                fixture["target"],
                material,
                output,
                fixture["root"],
                consumption_context=(
                    BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                ),
            )
            self.assertEqual(issue, "")
            self.assertEqual(
                producer_receipt["slot_index_space"],
                preflight.SOURCE_SPM_MAP_INDEX_SPACE,
            )
            producer_fallback = producer_receipt[
                "preview_role_fallbacks"
            ][0]
            self.assertEqual(producer_fallback["map_index"], 7)
            self.assertEqual(
                tuple(producer_fallback),
                FALLBACK_CANONICAL_FIELDS,
            )
            self.assertEqual(producer_receipt["version"], 2)
            self.assertEqual(
                producer_receipt["receipt_capabilities"],
                [PREVIEW_FALLBACK_CAPABILITY],
            )
            validate_preview_receipt(
                producer_receipt,
                requested_usage=PREVIEW_ONLY_USAGE,
            )

            readiness = preflight.augment_texture_readiness_contract(
                preflight.resolve_texture_bindings(fixture["stmat"]),
                fixture["stmat"],
                fixture["target"],
                source_texture_roots=[],
            )

            self.assertIn(
                readiness["status"],
                {"ok", "not_applicable", "nonblocking_diagnostics"},
                readiness,
            )
            self.assertEqual(readiness["missing"], [])
            receipt = readiness["bindings"][0]["origin_receipt"]
            fallback = receipt["preview_role_fallbacks"][0]
            self.assertEqual(
                fallback["usage"], "speedtree_preview_only"
            )
            self.assertEqual(
                fallback["slot_role"], "subsurfaceamount"
            )
            self.assertEqual(
                fallback["manifest_role"], "subsurfacecolor"
            )
            self.assertEqual(
                Path(fallback["path"]),
                fixture["paths"]["SubsurfaceColor"].resolve(),
            )
            self.assertEqual(
                receipt["slot_index_space"],
                preflight.STMAT_MAP_INDEX_SPACE,
            )
            self.assertEqual(fallback["map_index"], 6)
            self.assertEqual(tuple(fallback), FALLBACK_CANONICAL_FIELDS)
            second_readiness = (
                preflight.augment_texture_readiness_contract(
                    preflight.resolve_texture_bindings(fixture["stmat"]),
                    fixture["stmat"],
                    fixture["target"],
                    source_texture_roots=[],
                )
            )
            self.assertEqual(
                second_readiness["bindings"][0]["origin_receipt"],
                receipt,
            )
            self.assertEqual(fixture["target"].read_bytes(), before_bytes)
            self.assertEqual(
                fixture["target"].stat().st_mtime_ns,
                before_mtime_ns,
            )
            preserved = [
                row
                for row in preserved_cluster_materials(fixture["root"])
                if Path(row["spm"]).resolve()
                == fixture["target"].resolve()
            ]
            self.assertEqual(len(preserved), 1)
            self.assertEqual(
                Path(preserved[0]["source_spm"]).resolve(),
                fixture["source"].resolve(),
            )
            jobs = jobs_from_texture_plan(
                {"items": [], "preserved_cluster_materials": preserved},
                allowed_spms=[fixture["target"]],
            )
            patch = build_spm_patch(
                fixture["target"],
                jobs[0]["materials"],
                require_outputs=False,
                allow_partial_materials=True,
            )
            self.assertTrue(patch["changed"])
            repaired = inspect_material_slots(patch["text"])["2"]["slots"]
            self.assertEqual(
                repaired["subsurfaceamount"]["filename"],
                "cluster/cluster_Lauraceae_01_SubsurfaceAmount.tga",
            )
            self.assertNotEqual(
                repaired["subsurfaceamount"]["filename"],
                repaired["subsurfacecolor"]["filename"],
            )
            self.assertEqual(fixture["target"].read_bytes(), before_bytes)
            self.assertEqual(
                fixture["target"].stat().st_mtime_ns,
                before_mtime_ns,
            )

    def test_strict_context_still_rejects_the_preview_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(temporary)
            material, output = self.resolver_inputs(fixture)

            receipt, issue = resolve_blender_cluster_bake_origin(
                fixture["target"],
                material,
                output,
                fixture["root"],
            )

            self.assertEqual(receipt, {})
            self.assertEqual(issue, "blender_cluster_bake_map_role_mismatch")

    def test_preview_receipt_is_rejected_by_production_consumer(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(temporary)
            material, output = self.resolver_inputs(fixture)
            receipt, issue = resolve_blender_cluster_bake_origin(
                fixture["target"],
                material,
                output,
                fixture["root"],
                consumption_context=(
                    BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                ),
            )
            self.assertEqual(issue, "")
            self.assertEqual(
                validate_blender_cluster_bake_receipt_for_consumption(
                    receipt,
                    fixture["root"],
                ),
                "blender_cluster_bake_preview_fallback_forbidden",
            )
            with self.assertRaisesRegex(ValueError, "forbidden"):
                validate_preview_receipt(
                    receipt,
                    requested_usage="production_canonical",
                )
            legacy_path = fixture["cluster"] / "legacy_subsurface.tga"
            receipt["path_aliases"] = [{
                "legacy_path": str(legacy_path),
                "canonical_path": str(
                    fixture["paths"]["SubsurfaceColor"].resolve()
                ),
                "sha256": hashlib.sha256(
                    fixture["paths"]["SubsurfaceColor"].read_bytes()
                ).hexdigest(),
            }]
            self.assertIsNone(assembly._origin_alias_proof(
                legacy_path,
                fixture["paths"]["SubsurfaceColor"],
                [receipt],
            ))

    def test_unknown_schema_capability_and_digest_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(temporary)
            material, output = self.resolver_inputs(fixture)
            receipt, issue = resolve_blender_cluster_bake_origin(
                fixture["target"],
                material,
                output,
                fixture["root"],
                consumption_context=(
                    BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                ),
            )
            self.assertEqual(issue, "")
            cases = []
            bad_schema = dict(receipt)
            bad_schema["preview_role_fallbacks_schema_version"] = 99
            cases.append(bad_schema)
            bad_capability = dict(receipt)
            bad_capability["receipt_capabilities"] = ["unknown"]
            cases.append(bad_capability)
            bad_digest = dict(receipt)
            bad_digest["receipt_core_sha256"] = "0" * 64
            cases.append(bad_digest)
            for candidate in cases:
                with self.subTest(candidate=candidate):
                    self.assertEqual(
                        validate_blender_cluster_bake_receipt_for_consumption(
                            candidate,
                            fixture["root"],
                            consumption_context=(
                                BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                            ),
                        ),
                        "blender_cluster_bake_receipt_contract_invalid",
                    )
            unknown_envelope = dict(receipt)
            unknown_envelope.pop("preview_role_fallbacks", None)
            unknown_envelope.pop(
                "preview_role_fallbacks_schema_version", None
            )
            unknown_envelope.pop("receipt_claim", None)
            unknown_envelope["receipt_capabilities"] = ["unknown"]
            self.assertEqual(
                validate_blender_cluster_bake_receipt_for_consumption(
                    unknown_envelope,
                    fixture["root"],
                    consumption_context=(
                        BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                    ),
                ),
                "blender_cluster_bake_receipt_schema_unsupported",
            )
            output["origin_receipt"] = {
                "kind": "blender_cluster_bake_texture_origin_receipt",
                "version": 1,
                "physical_capture_manifest": str(fixture["manifest"]),
                "receipt_capabilities": ["unknown"],
            }
            rebuilt, rebuild_issue = resolve_blender_cluster_bake_origin(
                fixture["target"],
                material,
                output,
                fixture["root"],
                consumption_context=(
                    BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                ),
            )
            self.assertEqual(rebuilt, {})
            self.assertEqual(
                rebuild_issue,
                "blender_cluster_bake_receipt_schema_unsupported",
            )

    def test_unowned_preview_fallback_remains_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(
                temporary,
                amount_selection="unowned",
            )

            readiness = preflight.augment_texture_readiness_contract(
                preflight.resolve_texture_bindings(fixture["stmat"]),
                fixture["stmat"],
                fixture["target"],
                source_texture_roots=[],
            )

            self.assertEqual(readiness["status"], "incomplete")
            self.assertEqual(
                readiness["missing"][0]["origin_validation_issue"],
                "blender_cluster_bake_map_role_mismatch",
            )

    def test_cross_manifest_preview_fallback_remains_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(temporary)
            payload = json.loads(
                fixture["manifest"].read_text(encoding="utf-8")
            )
            selected = next(
                row
                for row in payload["maps"]
                if row["role"] == "SubsurfaceColor"
            )
            payload["maps"] = [
                row
                for row in payload["maps"]
                if row["role"] != "SubsurfaceColor"
            ]
            fixture["manifest"].write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            other = dict(payload)
            other["maps"] = [selected]
            (fixture["cluster"] / "other_auto_capture_manifest.json").write_text(
                json.dumps(other),
                encoding="utf-8",
            )
            material, output = self.resolver_inputs(fixture)

            receipt, issue = resolve_blender_cluster_bake_origin(
                fixture["target"],
                material,
                output,
                fixture["root"],
                consumption_context=(
                    BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                ),
            )

            self.assertEqual(receipt, {})
            self.assertEqual(issue, "blender_cluster_bake_map_role_mismatch")

    def test_fallback_does_not_search_an_alternate_amount_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(temporary)
            payload = json.loads(
                fixture["manifest"].read_text(encoding="utf-8")
            )
            payload["maps"] = [
                row
                for row in payload["maps"]
                if row["role"] != "SubsurfaceAmount"
            ]
            fixture["manifest"].write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            material, output = self.resolver_inputs(fixture)

            receipt, issue = resolve_blender_cluster_bake_origin(
                fixture["target"],
                material,
                output,
                fixture["root"],
                consumption_context=(
                    BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                ),
            )

            self.assertEqual(issue, "")
            self.assertEqual(
                receipt["preview_role_fallbacks"][0]["manifest_role"],
                "subsurfacecolor",
            )

    def test_core_role_swap_remains_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(
                temporary,
                normal_selection="color",
            )
            material, output = self.resolver_inputs(fixture)

            receipt, issue = resolve_blender_cluster_bake_origin(
                fixture["target"],
                material,
                output,
                fixture["root"],
                consumption_context=(
                    BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                ),
            )

            self.assertEqual(receipt, {})
            self.assertEqual(issue, "blender_cluster_bake_map_role_mismatch")

    def test_fallback_fails_closed_for_wrong_hash_root_material_and_index(self):
        cases = ("hash", "root", "material", "index")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = self.make_fixture(
                    temporary,
                    amount_selection=("outside" if case == "root" else "subsurface_color"),
                )
                material, output = self.resolver_inputs(fixture)
                if case == "hash":
                    fixture["paths"]["SubsurfaceColor"].write_bytes(
                        b"changed-after-manifest"
                    )
                elif case == "material":
                    self.assertFalse(cluster_render_origin_receipt(
                        fixture["root"],
                        fixture["target"],
                        [slot["authored_ref"] for slot in material["slots"]],
                        material_id="2",
                        material_name="M_wrong_material",
                    ))
                    continue
                elif case == "index":
                    amount = next(
                        row
                        for row in output["slot_files"]
                        if row["map"] == "SubsurfaceAmount"
                    )
                    amount["map_index"] += 100

                receipt, issue = resolve_blender_cluster_bake_origin(
                    fixture["target"],
                    material,
                    output,
                    fixture["root"],
                    consumption_context=(
                        BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                    ),
                )
                self.assertEqual(receipt, {})
                self.assertIn(issue, {
                    "blender_cluster_bake_capture_boundary_mismatch",
                    "blender_cluster_bake_file_fingerprint_mismatch",
                    "blender_cluster_bake_slot_contract_incomplete",
                })

    def test_missing_or_ambiguous_manifest_remains_blocked(self):
        cases = ("missing", "ambiguous")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = self.make_fixture(temporary)
                if case == "missing":
                    fixture["manifest"].unlink()
                else:
                    payload = json.loads(
                        fixture["manifest"].read_text(encoding="utf-8")
                    )
                    selected = next(
                        row
                        for row in payload["maps"]
                        if row["role"] == "SubsurfaceColor"
                    )
                    payload["maps"].append(dict(selected))
                    fixture["manifest"].write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                material, output = self.resolver_inputs(fixture)

                receipt, issue = resolve_blender_cluster_bake_origin(
                    fixture["target"],
                    material,
                    output,
                    fixture["root"],
                    consumption_context=(
                        BLENDER_BAKE_CONSUMPTION_SPEEDTREE_PREVIEW
                    ),
                )

                self.assertEqual(receipt, {})
                self.assertIn(issue, {
                    "blender_cluster_bake_capture_manifest_missing",
                    "blender_cluster_bake_capture_manifest_ambiguous",
                })


if __name__ == "__main__":
    unittest.main()
