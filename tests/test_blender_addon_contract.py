import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import blender_addon_contract as contract
import blender_addon_gateway as gateway


class BlenderAddonContractTests(unittest.TestCase):
    def test_manifest_assigns_non_overlapping_runtime_ownership(self):
        manifest = contract.integration_manifest()
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(
            set(manifest["ownership"]),
            {"batch", "gateway", "addon"},
        )
        self.assertIn(
            "job_queue_and_process_lifecycle",
            manifest["ownership"]["batch"]["owns"],
        )
        self.assertIn(
            "capability_negotiation",
            manifest["ownership"]["gateway"]["owns"],
        )
        self.assertIn(
            "blender_scene_and_datablock_mutation",
            manifest["ownership"]["addon"]["owns"],
        )

    def test_request_is_canonical_and_unknown_capability_fails_closed(self):
        first = contract.build_runtime_request(
            "test.job",
            {
                "send2ue": ["unreal_rpc_v1", "headless_export_v1"],
                "speedtree_bone_weight_repair": ["assembly_pipeline_v1"],
            },
        )
        second = contract.build_runtime_request(
            "test.job",
            {
                "speedtree_bone_weight_repair": ["assembly_pipeline_v1"],
                "send2ue": ["headless_export_v1", "unreal_rpc_v1"],
            },
        )
        self.assertEqual(first, second)
        with self.assertRaisesRegex(
            contract.AddonContractError, "unknown capabilities"
        ):
            contract.build_runtime_request(
                "test.job",
                {"send2ue": ["guess_whatever_is_installed"]},
            )

    def test_environment_source_expectations_are_explicit_only(self):
        values = {
            "SPEEDTREE_BWR_ADDON_DIR": r"C:\repo\bwr\addons\speedtree_bone_weight_repair",
            "UNRELATED": r"C:\ignored",
        }
        result = contract.source_expectations_from_environment(values)
        self.assertEqual(set(result), {"speedtree_bone_weight_repair"})
        self.assertTrue(
            result["speedtree_bone_weight_repair"].endswith(
                r"speedtree_bone_weight_repair"
            )
        )

    def test_installed_source_uses_blender_52_and_resolves_junction(self):
        with tempfile.TemporaryDirectory() as temporary:
            appdata = Path(temporary)
            older = (
                appdata
                / "Blender Foundation"
                / "Blender"
                / "4.3"
                / "scripts"
                / "addons"
                / "speedtree_bone_weight_repair"
            )
            selected = (
                appdata
                / "Blender Foundation"
                / "Blender"
                / "5.2"
                / "scripts"
                / "addons"
                / "speedtree_bone_weight_repair"
            )
            older.mkdir(parents=True)
            selected.mkdir(parents=True)
            self.assertEqual(
                contract.discover_installed_addon_source(
                    "speedtree_bone_weight_repair",
                    appdata=appdata,
                ),
                selected.resolve(),
            )

    def test_installed_source_does_not_fallback_to_blender_51(self):
        with tempfile.TemporaryDirectory() as temporary:
            appdata = Path(temporary)
            legacy = (
                appdata
                / "Blender Foundation"
                / "Blender"
                / "5.1"
                / "scripts"
                / "addons"
                / "speedtree_bone_weight_repair"
            )
            legacy.mkdir(parents=True)
            self.assertIsNone(
                contract.discover_installed_addon_source(
                    "speedtree_bone_weight_repair",
                    appdata=appdata,
                )
            )

    def test_receipt_is_bound_to_exact_request(self):
        request = contract.build_runtime_request(
            "test.job",
            {"speedtree_bone_weight_repair": ["assembly_pipeline_v1"]},
        )
        receipt = {
            "schema_version": 1,
            "status": "ready",
            "job": "test.job",
            "request_sha256": request["request_sha256"],
            "addons": [
                {
                    "id": "speedtree_bone_weight_repair",
                    "status": "ready",
                    "module_file": r"C:\addon\__init__.py",
                    "capabilities": ["assembly_pipeline_v1"],
                }
            ],
        }
        self.assertEqual(
            contract.validate_runtime_receipt(request, receipt), receipt
        )
        tampered = json.loads(json.dumps(receipt))
        tampered["request_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            contract.AddonContractError, "request identity mismatch"
        ):
            contract.validate_runtime_receipt(request, tampered)


class BlenderAddonGatewayTests(unittest.TestCase):
    def _fake_modules(self, package_file):
        loaded = set()
        addon_utils = types.ModuleType("addon_utils")

        def check(module_name):
            return False, module_name in loaded

        def enable(module_name, **_kwargs):
            loaded.add(module_name)
            return sys.modules[module_name]

        addon_utils.check = check
        addon_utils.enable = enable
        addon_utils.disable = lambda module_name, **_kwargs: loaded.discard(
            module_name
        )

        package = types.ModuleType("speedtree_bone_weight_repair")
        package.__file__ = str(package_file)
        package.__path__ = [str(package_file.parent)]
        package.bl_info = {"version": (9, 8, 7)}

        core = types.ModuleType("speedtree_bone_weight_repair.core")
        core.run_import_and_assemble = lambda value: ("assembled", value)
        core.run_speedtree_cli_export = lambda **kwargs: kwargs
        core.run_fresh_verification_only_export = lambda **kwargs: (
            "fresh_verification_only",
            kwargs,
        )
        core.run_fresh_collision_prune_export = lambda **kwargs: (
            "fresh_collision_prune",
            kwargs,
        )
        core.consolidate_speedtree_group_materials = lambda *args: args
        core.load_speedtree_texture_readiness_contract = lambda *args: args
        core.normalize_speedtree_material_textures = lambda *args: args
        return {
            "addon_utils": addon_utils,
            "speedtree_bone_weight_repair": package,
            "speedtree_bone_weight_repair.core": core,
        }

    def test_runtime_grants_only_negotiated_operations_and_emits_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            package_file = Path(temporary) / "__init__.py"
            package_file.write_text("# fake addon\n", encoding="utf-8")
            modules = self._fake_modules(package_file)
            with mock.patch.dict(sys.modules, modules, clear=False), mock.patch.dict(
                os.environ,
                {"SPEEDTREE_BWR_ADDON_DIR": temporary},
                clear=False,
            ):
                session = gateway.prepare_runtime(
                    "test.gateway",
                    {
                        "speedtree_bone_weight_repair": [
                            "assembly_pipeline_v1",
                        ]
                    },
                )
                operation = session.operation(
                    "speedtree_bone_weight_repair", "run_import_and_assemble"
                )
                self.assertEqual(operation({"tree": "spm"}), ("assembled", {"tree": "spm"}))
                row = session.receipt["addons"][0]
                self.assertEqual(row["addon_version"], "9.8.7")
                self.assertEqual(row["module_file"], str(package_file.resolve()))
                self.assertEqual(row["mode"], "gateway_adapter")
                with self.assertRaisesRegex(
                    gateway.BlenderAddonGatewayError, "was not granted"
                ):
                    session.operation(
                        "speedtree_bone_weight_repair",
                        "run_speedtree_cli_export",
                    )

    def test_fresh_verification_operation_requires_its_own_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            package_file = Path(temporary) / "__init__.py"
            package_file.write_text("# fake addon\n", encoding="utf-8")
            modules = self._fake_modules(package_file)
            with mock.patch.dict(sys.modules, modules, clear=False), mock.patch.dict(
                os.environ,
                {"SPEEDTREE_BWR_ADDON_DIR": temporary},
                clear=False,
            ):
                session = gateway.prepare_runtime(
                    "test.fresh_export",
                    {
                        "speedtree_bone_weight_repair": [
                            "fresh_verification_export_v1",
                        ]
                    },
                )
                operation = session.operation(
                    "speedtree_bone_weight_repair",
                    "run_fresh_verification_only_export",
                )
                self.assertEqual(
                    operation(force_reexport=True),
                    (
                        "fresh_verification_only",
                        {"force_reexport": True},
                    ),
                )
                with self.assertRaisesRegex(
                    gateway.BlenderAddonGatewayError,
                    "was not granted",
                ):
                    session.operation(
                        "speedtree_bone_weight_repair",
                        "run_speedtree_cli_export",
                    )

    def test_fresh_collision_prune_operation_requires_its_own_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            package_file = Path(temporary) / "__init__.py"
            package_file.write_text("# fake addon\n", encoding="utf-8")
            modules = self._fake_modules(package_file)
            with mock.patch.dict(sys.modules, modules, clear=False), mock.patch.dict(
                os.environ,
                {"SPEEDTREE_BWR_ADDON_DIR": temporary},
                clear=False,
            ):
                session = gateway.prepare_runtime(
                    "test.fresh_collision_export",
                    {
                        "speedtree_bone_weight_repair": [
                            "fresh_collision_prune_export_v1",
                        ]
                    },
                )
                operation = session.operation(
                    "speedtree_bone_weight_repair",
                    "run_fresh_collision_prune_export",
                )
                self.assertEqual(
                    operation(force_reexport=True),
                    (
                        "fresh_collision_prune",
                        {"force_reexport": True},
                    ),
                )
                with self.assertRaisesRegex(
                    gateway.BlenderAddonGatewayError,
                    "was not granted",
                ):
                    session.operation(
                        "speedtree_bone_weight_repair",
                        "run_fresh_verification_only_export",
                    )

    def test_explicit_source_mismatch_fails_before_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual" / "__init__.py"
            actual.parent.mkdir()
            actual.write_text("# fake addon\n", encoding="utf-8")
            expected = root / "expected"
            expected.mkdir()
            modules = self._fake_modules(actual)
            with mock.patch.dict(sys.modules, modules, clear=False), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                with self.assertRaisesRegex(
                    gateway.BlenderAddonGatewayError,
                    "differs from the configured source",
                ):
                    gateway.prepare_runtime(
                        "test.gateway",
                        {
                            "speedtree_bone_weight_repair": [
                                "assembly_pipeline_v1"
                            ]
                        },
                        expected_sources={
                            "speedtree_bone_weight_repair": expected
                        },
                    )


if __name__ == "__main__":
    unittest.main()
