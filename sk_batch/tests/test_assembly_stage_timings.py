import ast
import copy
import hashlib
import json
import tempfile
from pathlib import Path

from sk_batch.speedtree_native_receipt import (
    NativeReceiptError,
    load_native_export_receipt,
)


JOB_PATH = Path(__file__).resolve().parents[1] / "jobs" / "assembly_headless_job.py"


def load_record_stage_duration(now):
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "record_stage_duration"
    )
    namespace = {"perf_counter": lambda: now}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(JOB_PATH), "exec"),
        namespace,
    )
    return namespace["record_stage_duration"]


def load_reusable_contracts_helper():
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "reusable_preflight_spm_contracts"
    )
    namespace = {"copy": copy}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(JOB_PATH), "exec"),
        namespace,
    )
    return namespace["reusable_preflight_spm_contracts"]


def load_reusable_export_bundle_helper():
    tree = ast.parse(JOB_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "reusable_preflight_export_bundle"
    )
    namespace = {
        "copy": copy,
        "hashlib": hashlib,
        "json": json,
        "Path": Path,
        "NativeReceiptError": NativeReceiptError,
        "load_native_export_receipt": load_native_export_receipt,
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(JOB_PATH), "exec"),
        namespace,
    )
    return namespace["reusable_preflight_export_bundle"]


def attach_current_export_producer_cache(export, cache_path, cli, hook):
    def identity(path, *, include_hash):
        stat = path.stat()
        result = {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        if include_hash:
            result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    cache_path.write_text(
        json.dumps(
            {
                "version": 2,
                "input_fingerprint": export["input_fingerprint"],
                "inputs": {
                    "speedtree_exe": identity(cli, include_hash=False),
                    "speedtree_hook": identity(hook, include_hash=True),
                },
            }
        ),
        encoding="utf-8",
    )
    export["cache_path"] = str(cache_path)


def write_native_receipt(spm, path):
    stat = spm.stat()
    payload = {
        "schema_version": 2,
        "kind": "speedtree_native_export_receipt",
        "status": "ready",
        "source": {
            "path": str(spm.resolve()),
            "size": stat.st_size,
            "last_write_time_100ns": (
                stat.st_mtime_ns // 100 + 116444736000000000
            ),
        },
        "coordinate_contract": {
            "native_unit_to_meter": 0.3048,
            "blender_xyz_from_native_xyz": [
                "x*0.3048",
                "z*0.3048",
                "-y*0.3048",
            ],
        },
        "geometry_count": 1,
        "geometries": [{"ordinal": 0, "vertex_count": 1}],
        "bones": [],
        "generated_instances": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_stage_duration_uses_monotonic_elapsed_seconds():
    report = {}
    load_record_stage_duration(15.125)(report, "speedtree_export_bundle", 12.0)
    assert report["stage_timings_seconds"] == {
        "speedtree_export_bundle": 3.125
    }


def test_assembly_reports_export_and_total_stage_boundaries():
    source = JOB_PATH.read_text(encoding="utf-8")
    for stage in (
        "input_preflight",
        "addon_runtime_prepare",
        "blend_open",
        "speedtree_export_bundle",
        "blender_import_and_assemble",
        "post_assembly_spm_contracts",
        "vertex_payload_finalize",
        "blend_save",
        "blend_identity_fingerprint",
        "pipeline_report_write",
        "total_job",
    ):
        assert f'"{stage}"' in source


def test_exact_preflight_export_reuses_leaf_and_material_contracts():
    helper = load_reusable_contracts_helper()
    artifact = {
        "relative_path": "Tree.fbx",
        "size": 123,
        "sha256": "abc",
    }
    preflight = {
        "status": "ok",
        "speedtree_export": {
            "input_fingerprint": "input-1",
            "artifacts": [artifact],
        },
        "leaf_reference_contract": {"status": "source_only"},
        "material_export_contract": {"status": "ok"},
    }
    current = {
        "input_fingerprint": "input-1",
        "artifacts": [dict(artifact)],
    }

    result = helper(preflight, current)

    assert result == (
        {"status": "source_only"},
        {"status": "ok"},
    )
    assert result[0] is not preflight["leaf_reference_contract"]


def test_changed_exact_export_falls_back_to_live_spm_inspection():
    helper = load_reusable_contracts_helper()
    preflight = {
        "status": "ok",
        "speedtree_export": {
            "input_fingerprint": "old",
            "artifacts": [{
                "relative_path": "Tree.fbx",
                "size": 123,
                "sha256": "abc",
            }],
        },
        "leaf_reference_contract": {"status": "source_only"},
        "material_export_contract": {"status": "ok"},
    }
    current = {
        "input_fingerprint": "new",
        "artifacts": [{
            "relative_path": "Tree.fbx",
            "size": 123,
            "sha256": "abc",
        }],
    }

    assert helper(preflight, current) is None


def test_assembly_reuses_hash_verified_preflight_bundle_without_exporter():
    helper = load_reusable_export_bundle_helper()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        spm = root / "SK_fern.spm"
        fbx = root / "fbx" / "SK_fern.fbx"
        stmat = root / "fbx" / "SK_fern.stmat"
        receipt = root / "fbx" / "SK_fern.speedtree_native_receipt.json"
        xml = root / "xml" / "SK_fern.xml"
        cli = root / "SpeedTreeCLI.exe"
        hook = root / "speedtree_hook.dll"
        spm.write_bytes(b"spm")
        fbx.parent.mkdir()
        xml.parent.mkdir()
        fbx.write_bytes(b"fbx-with-native-skin")
        stmat.write_bytes(b"materials")
        xml.write_bytes(b"<SpeedTree />")
        cli.write_bytes(b"cli")
        hook.write_bytes(b"hook")
        write_native_receipt(spm, receipt)
        assert load_native_export_receipt(
            receipt,
            source_spm=spm,
        )["status"] == "ready"

        def artifact(path):
            return {
                "relative_path": path.name,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        preflight = {
            "status": "ok",
            "speedtree_export": {
                "path": str(fbx),
                "exists": True,
                "input_fingerprint": "fbx-input",
                "native_receipt": str(receipt),
                "artifacts": [artifact(fbx), artifact(stmat)],
            },
            "speedtree_xml_export": {
                "path": str(xml),
                "exists": True,
                "input_fingerprint": "xml-input",
                "artifacts": [artifact(xml)],
            },
            "speedtree_native_receipt": {
                "status": "ready",
                "path": str(receipt),
            },
        }

        attach_current_export_producer_cache(
            preflight["speedtree_export"],
            root / "fbx_export_cache.json",
            cli,
            hook,
        )
        attach_current_export_producer_cache(
            preflight["speedtree_xml_export"],
            root / "xml_export_cache.json",
            cli,
            hook,
        )

        result = helper(preflight, spm, cli, hook)

        assert result["process_started"] is False
        assert result["assembly_preflight_reuse"] is True
        assert result["native_receipt_verified"] is True
        assert result["exports"]["fbx"]["path"] == str(fbx)
        assert result["exports"]["fbx"]["cache_hit"] is True
        assert result["exports"]["xml"]["cache_hit"] is True


def test_assembly_rejects_preflight_bundle_after_artifact_drift():
    helper = load_reusable_export_bundle_helper()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        spm = root / "SK_fern.spm"
        fbx = root / "fbx" / "SK_fern.fbx"
        receipt = root / "fbx" / "SK_fern.speedtree_native_receipt.json"
        xml = root / "xml" / "SK_fern.xml"
        cli = root / "SpeedTreeCLI.exe"
        hook = root / "speedtree_hook.dll"
        spm.write_bytes(b"spm")
        fbx.parent.mkdir()
        xml.parent.mkdir()
        fbx.write_bytes(b"original")
        xml.write_bytes(b"xml")
        cli.write_bytes(b"cli")
        hook.write_bytes(b"hook")
        write_native_receipt(spm, receipt)
        preflight = {
            "status": "ok",
            "speedtree_export": {
                "path": str(fbx),
                "input_fingerprint": "fbx-input",
                "native_receipt": str(receipt),
                "artifacts": [{
                    "relative_path": fbx.name,
                    "size": fbx.stat().st_size,
                    "sha256": hashlib.sha256(fbx.read_bytes()).hexdigest(),
                }],
            },
            "speedtree_xml_export": {
                "path": str(xml),
                "input_fingerprint": "xml-input",
                "artifacts": [{
                    "relative_path": xml.name,
                    "size": xml.stat().st_size,
                    "sha256": hashlib.sha256(xml.read_bytes()).hexdigest(),
                }],
            },
            "speedtree_native_receipt": {
                "status": "ready",
                "path": str(receipt),
            },
        }
        attach_current_export_producer_cache(
            preflight["speedtree_export"],
            root / "fbx_export_cache.json",
            cli,
            hook,
        )
        attach_current_export_producer_cache(
            preflight["speedtree_xml_export"],
            root / "xml_export_cache.json",
            cli,
            hook,
        )
        fbx.write_bytes(b"drifted!")

        assert helper(preflight, spm, cli, hook) is None
