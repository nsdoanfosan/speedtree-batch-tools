import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cluster_source_prepare as source_prepare


class ClusterSourceCollisionCliTests(unittest.TestCase):
    def test_material_preflight_uses_configured_collision_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            cli = Path(temporary) / "speedtree_collision_cli.exe"
            cli.write_bytes(b"test collision cli")

            with mock.patch.dict(
                os.environ,
                {source_prepare.COLLISION_CLI_ENV: str(cli)},
                clear=False,
            ):
                resolved = source_prepare._material_preflight_export_executable()

            self.assertEqual(resolved, cli.resolve())

    def test_material_preflight_rejects_plain_modeler_as_collision_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            modeler = Path(temporary) / "SpeedTree_Modeler.exe"
            modeler.write_bytes(b"stock modeler")

            with mock.patch.dict(
                os.environ,
                {source_prepare.COLLISION_CLI_ENV: str(modeler)},
                clear=False,
            ):
                with self.assertRaises(
                    source_prepare.ClusterSourcePreparationError
                ) as raised:
                    source_prepare._material_preflight_export_executable()

            self.assertEqual(raised.exception.stage, "material_preflight")
            self.assertIn("speedtree_collision_cli.exe", str(raised.exception))

    def test_material_preflight_uses_repo_cli_when_environment_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            cli = (
                repo
                / "speedtree_collision_cli"
                / "bin"
                / "speedtree_collision_cli.exe"
            )
            cli.parent.mkdir(parents=True)
            cli.write_bytes(b"test collision cli")

            with mock.patch.object(source_prepare, "REPO_DIR", repo), mock.patch.dict(
                os.environ,
                {source_prepare.COLLISION_CLI_ENV: ""},
                clear=False,
            ):
                resolved = source_prepare._material_preflight_export_executable()

            self.assertEqual(resolved, cli.resolve())


if __name__ == "__main__":
    unittest.main()
