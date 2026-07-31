import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path

from cluster_card_pipeline.capture_refresh import (
    REQUIRED_CAPTURE_MAPS,
    begin_camera_capture_request,
    ensure_camera_capture_refresh,
    finalize_camera_capture_request,
    validate_camera_capture_receipt,
)
from cluster_card_pipeline.contract import ContractError, _fingerprint


def write_tga(path, width=2, height=2, marker=0):
    header = bytearray(18)
    header[2] = 2
    header[12:14] = int(width).to_bytes(2, "little")
    header[14:16] = int(height).to_bytes(2, "little")
    header[16] = 24
    Path(path).write_bytes(bytes(header) + bytes([marker]) * width * height * 3)


class CaptureRefreshTests(unittest.TestCase):
    def make_fixture(self, root):
        root = Path(root)
        camera_spm = root / "leaf_elm_01.spm"
        camera_spm.write_bytes(gzip.compress(b"<SpeedTree><Assets /></SpeedTree>", mtime=0))
        maps = {}
        for index, role in enumerate(REQUIRED_CAPTURE_MAPS):
            texture = root / f"leaf_elm_01_{role}.tga"
            write_tga(texture, marker=index)
            maps[role] = {
                "path": str(texture),
                "size": [2, 2],
                "actual_size": [2, 2],
            }
        contract = {
            "camera_spm": _fingerprint(camera_spm),
            "camera": {
                "name": "Ortho camera",
                "guid": "camera-guid",
                "translation": [0.0, 0.0, 0.0],
                "rotation_axis": [0.0, 0.0, 1.0],
                "rotation_angle_degrees": 0.0,
                "width": 5.6,
                "height": 5.6,
            },
            "material": {
                "id": 6,
                "name": "M_leaf_elm_01",
                "width": 2,
                "height": 2,
                "maps": maps,
            },
        }
        manifest = root / "leaf_elm_01_normalization_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "kind": "speedtree_cluster_card_camera_projection",
                    **contract,
                }
            ),
            encoding="utf-8",
        )
        return camera_spm, contract, manifest

    def rewrite_all_maps_after_request(self, request_result):
        request = request_result["request"]
        rewritten_ns = int(request["created_at_ns"]) + 2_000_000_000
        for index, row in enumerate(request["maps_before_capture"]):
            texture = Path(row["path"])
            write_tga(texture, marker=100 + index)
            os.utime(texture, ns=(rewritten_ns, rewritten_ns))

    def test_finalize_rejects_maps_not_exported_after_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            camera_spm, contract, manifest = self.make_fixture(temporary)
            started = begin_camera_capture_request(
                contract,
                camera_spm,
                manifest,
            )
            with self.assertRaisesRegex(ContractError, "Waiting for"):
                finalize_camera_capture_request(started["request_path"])

    def test_finalize_and_validate_complete_color_opacity_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            camera_spm, contract, manifest = self.make_fixture(temporary)
            started = begin_camera_capture_request(
                contract,
                camera_spm,
                manifest,
            )
            self.rewrite_all_maps_after_request(started)
            finalized = finalize_camera_capture_request(started["request_path"])
            receipt = validate_camera_capture_receipt(
                finalized["receipt_path"],
                contract,
                camera_spm,
            )
            self.assertTrue(receipt["all_maps_rewritten_after_request"])
            self.assertEqual(
                receipt["changed_content_roles"],
                sorted(REQUIRED_CAPTURE_MAPS),
            )

    def test_ensure_consumes_receipt_and_refreshes_manifest_fingerprints(self):
        with tempfile.TemporaryDirectory() as temporary:
            camera_spm, contract, manifest = self.make_fixture(temporary)
            started = begin_camera_capture_request(
                contract,
                camera_spm,
                manifest,
            )
            self.rewrite_all_maps_after_request(started)
            finalize_camera_capture_request(started["request_path"])
            refreshed = ensure_camera_capture_refresh(
                contract,
                camera_spm,
                manifest,
            )
            self.assertEqual(refreshed["status"], "ready")
            self.assertIn("camera_capture_receipt", refreshed["contract"])
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["camera_capture_receipt"]["sha256"],
                refreshed["receipt"]["receipt_sha256"],
            )
            for role in REQUIRED_CAPTURE_MAPS:
                self.assertEqual(
                    saved["material"]["maps"][role]["sha256"],
                    next(
                        row["sha256"]
                        for row in refreshed["receipt"]["textures"]
                        if row["role"] == role
                    ),
                )

    def test_receipt_rejects_camera_spm_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            camera_spm, contract, manifest = self.make_fixture(temporary)
            started = begin_camera_capture_request(
                contract,
                camera_spm,
                manifest,
            )
            self.rewrite_all_maps_after_request(started)
            finalized = finalize_camera_capture_request(started["request_path"])
            camera_spm.write_bytes(
                gzip.compress(b"<SpeedTree><Assets /><Changed /></SpeedTree>", mtime=0)
            )
            with self.assertRaisesRegex(ContractError, "changed after"):
                validate_camera_capture_receipt(
                    finalized["receipt_path"],
                    contract,
                    camera_spm,
                )


if __name__ == "__main__":
    unittest.main()
