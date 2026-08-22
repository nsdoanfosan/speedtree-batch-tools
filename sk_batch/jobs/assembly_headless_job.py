"""Headless Blender job: SPM -> SpeedTree CLI export -> Assembly -> .blend.

Run:
  blender.exe -b --python assembly_headless_job.py -- --spm X.spm --blend X.blend
      --wind TREE|BUSH|WEED|NONE --material-contract preflight.json
      --report result.json

Runs with --factory-startup and enables only the junction-installed
speedtree_bone_weight_repair add-on for this process.  This avoids loading every
interactive user add-on in a background batch. Re-running on an existing .blend
is a clean idempotent update (the operator wipes its previous build).
"""
import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from time import perf_counter

import bpy

JOBS_DIR = str(Path(__file__).resolve().parent)
if JOBS_DIR not in sys.path:
    sys.path.insert(0, JOBS_DIR)
SK_BATCH_DIR = str(Path(__file__).resolve().parent.parent)
if SK_BATCH_DIR not in sys.path:
    sys.path.insert(0, SK_BATCH_DIR)
BATCH_TOOLS_DIR = str(Path(__file__).resolve().parent.parent.parent)
if BATCH_TOOLS_DIR not in sys.path:
    sys.path.insert(0, BATCH_TOOLS_DIR)

DEFAULT_SPEEDTREE_COLLISION_CLI = (
    Path(BATCH_TOOLS_DIR)
    / "speedtree_collision_cli"
    / "bin"
    / "speedtree_collision_cli.exe"
)


def resolve_speedtree_collision_cli():
    """Return the version-locked post-collision exporter used by Assembly."""
    configured = os.environ.get("SPEEDTREE_COLLISION_CLI_EXE")
    executable = (
        Path(configured).expanduser()
        if configured
        else DEFAULT_SPEEDTREE_COLLISION_CLI
    ).resolve()
    hook = executable.with_name("speedtree_collision_hook.dll")
    if not executable.is_file() or not hook.is_file():
        raise RuntimeError(
            "SpeedTree collision CLI is not built. Run "
            "speedtree_collision_cli\\build.ps1 before SK Batch: "
            f"{executable}"
        )
    return executable, hook

from vertex_color_contract import pack_speedtree_vertex_payload
from assembly_export_evidence import export_object_postcondition
from speedtree_pipeline_contract import (
    refresh_preflight_report_after_exact_export,
    reuse_preflight_report_after_unchanged_export,
    source_identity,
    validate_preflight_report,
)
from assembly_atlas_manifest_bridge import install_assembly_atlas_manifest_resolver
from cluster_assembly_handoff_contract import (
    assembly_source_fbx_resolution,
    build_assembly_handoff,
    build_blender_fbx_inventory,
    current_assembly_manifest_handoff,
    file_fingerprint,
    load_cluster_contract,
    resolve_cluster_receipt_path,
    role_identity_aliases_from_contract,
)
from cluster_assembly_builder import build_blender_assembly_inputs
from job_report_contract import mark_job_failed
from assembly_runtime_contract import ASSEMBLY_OUTPUT_CONTRACT_VERSION
from cluster_export_handoff_contract import (
    capture_cluster_export_snapshot,
    cluster_export_contract_issues as inspect_cluster_export_contract,
    reconcile_transient_cluster_export_root,
)
from pcg_st9_texture_batch.pcg_cluster_bark_normalization import (
    BarkNormalizationError,
    validate_canonical_bark_export_bundle,
)
from cluster_bark_source_resolution import (
    ClusterBarkSourceResolutionError,
    load_current_isolated_bark_manifest,
)
from blender_addon_gateway import prepare_runtime
from speedtree_native_receipt import (
    NativeReceiptError,
    load_native_export_receipt,
)


def is_cluster_normalization_spm(spm_path):
    path = Path(spm_path)
    return (
        path.parent.name.casefold() == "cluster"
        or path.stem.casefold().startswith("sk_cluster_")
    )


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--spm", required=True)
    parser.add_argument("--speedtree-spm", default="")
    parser.add_argument("--blend", required=True)
    parser.add_argument(
        "--wind",
        default="WEED",
        choices=["TREE", "BUSH", "WEED", "NONE", "GRASS"],
        help="Immutable response preset ID (legacy GRASS is accepted as WEED)",
    )
    parser.add_argument("--material-contract", required=True)
    parser.add_argument("--bark-normalization-manifest", default="")
    parser.add_argument("--cluster-source-build-only", action="store_true")
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def write_report(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def record_stage_duration(report, name, started):
    """Attach one monotonic stage duration without changing job control flow."""
    report.setdefault("stage_timings_seconds", {})[name] = round(
        perf_counter() - started,
        6,
    )


def compact_cluster_assembly_handoff(handoff):
    """Keep routing facts in reports and leave geometry evidence at its source."""

    if not isinstance(handoff, dict):
        return None
    summary = {
        key: copy.deepcopy(handoff[key])
        for key in (
            "schema_version",
            "kind",
            "status",
            "content_decision",
            "pcg_receipt",
            "spm",
            "actual_fbx",
            "role_demotions",
        )
        if key in handoff
    }
    assembly = handoff.get("assembly") or {}
    part_inputs = list(assembly.get("part_builder_inputs") or ())
    summary["assembly_summary"] = {
        "part_builder_input_count": len(part_inputs),
        "provider_keys": sorted({
            str(row.get("provider_key") or "")
            for row in part_inputs
            if isinstance(row, dict) and row.get("provider_key")
        }),
    }
    return summary


def compact_cluster_assembly_manifest(manifest):
    """Reference the authoritative bindings file instead of nesting it twice."""

    if not isinstance(manifest, dict):
        return None
    summary = {
        key: copy.deepcopy(manifest[key])
        for key in (
            "schema_version",
            "kind",
            "status",
            "content_decision",
            "full_asset_stem",
            "manifest",
            "production_build_manifest_preserved",
            "existing_assembly_assets_orphaned",
            "cache_reused",
        )
        if key in manifest
    }
    parts = list(manifest.get("parts") or ())
    summary["part_count"] = len(parts)
    summary["binding_count"] = sum(
        len((part or {}).get("bindings") or ())
        for part in parts
        if isinstance(part, dict)
    )
    summary["registered_variant_count"] = len(
        list(manifest.get("registered_variants") or ())
    )
    return summary


def reusable_preflight_spm_contracts(material_preflight, current_fbx_export):
    """Reuse leaf/material inspection only for the exact exported artifacts."""
    if not isinstance(material_preflight, dict):
        return None
    if material_preflight.get("status") != "ok":
        return None
    previous_export = material_preflight.get("speedtree_export") or {}
    if not isinstance(previous_export, dict) or not isinstance(
        current_fbx_export, dict
    ):
        return None

    def artifact_signature(export):
        rows = []
        for artifact in export.get("artifacts") or ():
            if not isinstance(artifact, dict):
                return ()
            rows.append((
                str(artifact.get("relative_path") or "").casefold(),
                int(artifact.get("size") or 0),
                str(artifact.get("sha256") or "").casefold(),
            ))
        return tuple(sorted(rows))

    previous_signature = artifact_signature(previous_export)
    current_signature = artifact_signature(current_fbx_export)
    if not previous_signature or previous_signature != current_signature:
        return None
    if str(previous_export.get("input_fingerprint") or "") != str(
        current_fbx_export.get("input_fingerprint") or ""
    ):
        return None
    leaf = material_preflight.get("leaf_reference_contract")
    material = material_preflight.get("material_export_contract")
    if not isinstance(leaf, dict) or not isinstance(material, dict):
        return None
    return copy.deepcopy(leaf), copy.deepcopy(material)


def reusable_preflight_export_bundle(material_preflight, speedtree_spm):
    """Return the exact preflight FBX/XML without reacquiring SpeedTree.

    ``validate_preflight_report`` has already proven that the report belongs to
    the current SPM/STMAT snapshot.  This final check hashes every recorded
    producer artifact so Assembly cannot reuse an output that changed after
    preflight.  A missing historical field or any drift falls back to the
    installed export helper and its normal cache/producer validation.
    """
    if not isinstance(material_preflight, dict):
        return None
    if material_preflight.get("status") != "ok":
        return None

    spm = Path(speedtree_spm).resolve()
    expected_paths = {
        "fbx": (spm.parent / "fbx" / f"{spm.stem}.fbx").resolve(),
        "xml": (spm.parent / "xml" / f"{spm.stem}.xml").resolve(),
    }
    recorded = {
        "fbx": material_preflight.get("speedtree_export"),
        "xml": material_preflight.get("speedtree_xml_export"),
    }
    receipt_summary = material_preflight.get("speedtree_native_receipt")
    fbx_record = recorded["fbx"]
    if (
        not isinstance(receipt_summary, dict)
        or receipt_summary.get("status") != "ready"
        or not isinstance(fbx_record, dict)
    ):
        return None
    receipt_path_value = str(receipt_summary.get("path") or "")
    export_receipt_value = str(fbx_record.get("native_receipt") or "")
    if not receipt_path_value or not export_receipt_value:
        return None
    try:
        receipt_path = Path(receipt_path_value).resolve()
        export_receipt_path = Path(export_receipt_value).resolve()
        if receipt_path != export_receipt_path:
            return None
        load_native_export_receipt(receipt_path, source_spm=spm)
    except (NativeReceiptError, OSError, RuntimeError, ValueError):
        return None

    def sha256_file(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    exports = {}
    for format_name, expected_path in expected_paths.items():
        export = recorded.get(format_name)
        if not isinstance(export, dict):
            return None
        recorded_path_value = str(export.get("path") or "")
        input_fingerprint = str(export.get("input_fingerprint") or "")
        artifacts = export.get("artifacts")
        if (
            not recorded_path_value
            or not input_fingerprint
            or not isinstance(artifacts, list)
            or not artifacts
        ):
            return None
        try:
            recorded_path = Path(recorded_path_value).resolve()
        except (OSError, RuntimeError):
            return None
        if recorded_path != expected_path or not expected_path.is_file():
            return None

        for artifact in artifacts:
            if not isinstance(artifact, dict):
                return None
            relative_path = str(artifact.get("relative_path") or "")
            recorded_size = int(artifact.get("size") or 0)
            recorded_sha256 = str(artifact.get("sha256") or "").casefold()
            if not relative_path or recorded_size <= 0 or not recorded_sha256:
                return None
            try:
                artifact_path = (recorded_path.parent / relative_path).resolve()
                artifact_path.relative_to(recorded_path.parent)
            except (OSError, RuntimeError, ValueError):
                return None
            if not artifact_path.is_file():
                return None
            try:
                if artifact_path.stat().st_size != recorded_size:
                    return None
                if sha256_file(artifact_path).casefold() != recorded_sha256:
                    return None
            except OSError:
                return None

        reused = copy.deepcopy(export)
        reused["cache_hit"] = True
        reused["assembly_preflight_reuse"] = True
        exports[format_name] = reused

    return {
        "status": "ok",
        "exports": exports,
        "process_started": False,
        "assembly_preflight_reuse": True,
        "native_receipt_verified": True,
    }


def save_cluster_source_mainfile(bpy_module, filepath, report):
    """Save one Cluster source without Blender's redundant version rename.

    The GUI owns a separate pre-assembly copy and restores it if this producer
    transaction fails.  Blender's additional ``.blend -> .blend1`` rename is
    therefore redundant here and can fail when OneDrive briefly holds either
    path.  Change the preference only in memory for this operator call and
    restore it even when Blender raises.
    """
    filepaths = bpy_module.context.preferences.filepaths
    original_save_version = int(filepaths.save_version)
    policy = {
        "status": "applying",
        "policy": (
            "gui_pre_assembly_transaction_authoritative_disable_blender_"
            "version_backup"
        ),
        "scope": "headless_cluster_source_final_save",
        "preference": "bpy.context.preferences.filepaths.save_version",
        "original_save_version": original_save_version,
        "effective_save_version": 0,
        "preference_persisted": False,
        "preference_restored": False,
        "transaction_backup": "sk_batch_gui_pre_assembly_copy_and_rollback",
    }
    report["blend_save_policy"] = policy
    try:
        filepaths.save_version = 0
        policy["observed_effective_save_version"] = int(
            filepaths.save_version
        )
        result = bpy_module.ops.wm.save_as_mainfile(filepath=filepath)
        policy["operator_result"] = sorted(result)
        policy["status"] = (
            "committed" if "FINISHED" in result else "operator_incomplete"
        )
        return result
    except Exception as exc:
        policy["status"] = "operator_failed"
        policy["error_type"] = type(exc).__name__
        policy["error"] = str(exc)
        raise
    finally:
        filepaths.save_version = original_save_version
        policy["restored_save_version"] = int(filepaths.save_version)
        policy["preference_restored"] = (
            int(filepaths.save_version) == original_save_version
        )


def atomic_copy(source, destination):
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def remove_unused_empty_material_slots(obj):
    if obj is None or obj.type != "MESH" or not obj.data:
        return []
    removed = []
    for slot_index in range(len(obj.data.materials) - 1, -1, -1):
        if obj.data.materials[slot_index] is not None:
            continue
        used_faces = sum(
            1 for polygon in obj.data.polygons
            if polygon.material_index == slot_index
        )
        if used_faces:
            continue
        obj.data.materials.pop(index=slot_index)
        removed.append({"object": obj.name, "slot": slot_index})
    return removed


def export_collection_contract_issues(cluster_source_stem=""):
    """Return Send2UE units that would become unintended standalone assets."""
    if not cluster_source_stem:
        export_collection = bpy.data.collections.get("Export")
        if export_collection is None:
            return ["missing_export_collection"]
        return [
            f"orphan_owned_export_empty:{obj.name}"
            for obj in export_collection.objects
            if obj.type == "EMPTY"
            and not obj.children
            and bool(obj.get("codex_source_fbx", ""))
        ]
    return inspect_cluster_export_contract(
        bpy.data,
        cluster_source_stem,
    )


def inspect_cluster_assembly_fbx(receipt_path, spm_path, source_fbx_path):
    """Import the exact FBX in-memory and reconcile it before Assembly saves.

    The Assembly operator clears these tagged objects and performs its normal
    clean import.  If the contract blocks, this background Blender process exits
    without saving, so an existing user-managed Full SK blend stays untouched.
    """
    _payload, contract = load_cluster_contract(receipt_path, spm_path)
    role_identities = role_identity_aliases_from_contract(
        contract,
        spm_path,
    )
    data_collections = ("meshes", "armatures", "materials", "images", "actions")
    before_objects = {obj.as_pointer() for obj in bpy.data.objects}
    before_data = {
        name: {value.as_pointer() for value in getattr(bpy.data, name)}
        for name in data_collections
    }
    imported = []
    try:
        result = bpy.ops.import_scene.fbx(
            filepath=str(Path(source_fbx_path).resolve())
        )
        if "FINISHED" not in result:
            raise RuntimeError(f"Assembly FBX inspection import returned {result}")
        imported = [
            obj for obj in bpy.data.objects
            if obj.as_pointer() not in before_objects
        ]
        for obj in imported:
            obj["codex_source_fbx"] = str(Path(source_fbx_path).resolve())
        inventory = build_blender_fbx_inventory(
            imported,
            source_fbx_path,
            role_identities,
        )
        return build_assembly_handoff(receipt_path, spm_path, inventory)
    finally:
        for obj in list(imported):
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        for name in data_collections:
            collection = getattr(bpy.data, name)
            for value in list(collection):
                if (
                    value.as_pointer() not in before_data[name]
                    and value.users == 0
                ):
                    collection.remove(value)


def require_cluster_assembly_handoff_ready(handoff):
    if handoff.get("status") != "blocked":
        return

    def describe(item):
        fields = [str(item.get("code") or "CLUSTER_HANDOFF_BLOCKED")]
        if item.get("role"):
            fields.append(f"role={item['role']}")
        if item.get("spm"):
            fields.append(f"provider={item['spm']}")
        elif item.get("artifact"):
            fields.append(f"artifact={item['artifact']}")
        if item.get("material"):
            fields.append(f"material={item['material']}")
        if item.get("canonical_material"):
            fields.append(
                f"canonical_material={item['canonical_material']}"
            )
        if item.get("reason"):
            fields.append(f"reason={item['reason']}")
        details = item.get("details") or {}
        if details.get("status"):
            fields.append(f"status={details['status']}")
        missing = list(details.get("missing") or [])
        invalid = list(details.get("invalid") or [])
        if missing:
            fields.append("missing=" + ", ".join(missing[:3]))
        if invalid:
            fields.append("invalid=" + ", ".join(invalid[:3]))
        conflicts = []
        for conflict in item.get("canonical_conflicts") or []:
            providers = [
                Path(value).name
                for value in conflict.get("providers") or []
            ]
            textures = list(conflict.get("texture_basenames") or [])
            conflicts.append(
                (",".join(providers) or "?")
                + "["
                + ",".join(textures)
                + "]"
            )
        if conflicts:
            fields.append("conflicts=" + " vs ".join(conflicts))
        return " ".join(fields)

    reasons = "; ".join(
        describe(item) for item in handoff.get("issues") or []
    )
    raise RuntimeError(
        "PCG Cluster Assembly handoff blocked before Blender Assembly: "
        + (reasons or "unknown contract error")
    )


def select_cluster_assembly_build_handoff(
    receipt_contract,
    inspected_handoff,
    current_manifest_handoff=None,
):
    """Prefer every conclusive current-FBX result over persisted fallbacks.

    The PCG receipt describes what was known before the final Assembly FBX.
    Once that FBX has been inspected, both ``ready`` and ``pass_through`` are
    authoritative content decisions.  A production build manifest is only a
    recovery source when current inspection produced no conclusive result; it
    must never resurrect Assembly for a current pass-through FBX.
    """
    if (
        isinstance(inspected_handoff, dict)
        and inspected_handoff.get("status") == "ready"
    ):
        return "build", inspected_handoff
    if (
        isinstance(inspected_handoff, dict)
        and inspected_handoff.get("status") == "pass_through"
    ):
        return "pass_through", inspected_handoff
    if (
        isinstance(current_manifest_handoff, dict)
        and current_manifest_handoff.get("status") == "ready"
    ):
        return "build", current_manifest_handoff

    receipt_handoff = {}
    if isinstance(receipt_contract, dict):
        receipt_handoff = receipt_contract.get("handoff") or {}
    if receipt_handoff.get("status") == "pass_through":
        return "pass_through", receipt_handoff
    if isinstance(inspected_handoff, dict):
        return "build", inspected_handoff
    return None, None


def cluster_assembly_contract_from_material_contract(receipt_path, spm_path):
    """Find the additive PCG receipt inside the existing required contract."""
    try:
        _payload, contract = load_cluster_contract(receipt_path, spm_path)
    except ValueError as exc:
        if str(exc) == "PCG receipt contains no cluster_assembly contract":
            return None
        raise
    return contract


def main():
    job_started = perf_counter()
    args = parse_args()
    canonical_spm = Path(args.spm).resolve()
    speedtree_spm = Path(args.speedtree_spm or args.spm).resolve()
    report = {
        "spm": str(canonical_spm),
        "speedtree_spm": str(speedtree_spm),
        "blend": args.blend,
        "wind": args.wind,
        "status": "failed",
    }
    # Cluster rows reach this job under their canonical SK_ output identity.
    # ``--speedtree-spm`` can additionally point at an immutable isolated copy
    # when the Assembly receipt requires canonical bark before Atlas capture.
    # Legacy lineage never affects Assembly, so do not spend tens of seconds
    # parsing a large SPM solely to populate historical diagnostics.
    source_review_policy = "diagnostic_only"
    report["source_review_policy"] = source_review_policy
    report["legacy_cluster_lineage"] = {
        "status": "diagnostic_not_run",
        "policy": "legacy_lineage_is_not_an_assembly_or_export_input",
        "receipt": "",
        "receipt_valid": False,
        "generator_count": 0,
        "generator_guids": [],
        "marker_drift_guids": [],
        "errors": [],
    }
    try:
        preflight_started = perf_counter()
        bark_normalization_manifest = None
        if args.bark_normalization_manifest:
            manifest_path = Path(
                args.bark_normalization_manifest
            ).resolve()
            try:
                bark_normalization_manifest = (
                    load_current_isolated_bark_manifest(
                        manifest_path,
                        source_spm=canonical_spm,
                        speedtree_spm=speedtree_spm,
                    )
                )
            except ClusterBarkSourceResolutionError as exc:
                raise RuntimeError(
                    "Cluster bark normalization manifest is stale or "
                    "incompatible with the exact requested provider: "
                    f"{manifest_path}: {exc}"
                ) from exc
            report["cluster_bark_source_resolution"] = {
                "status": "ready",
                "manifest": file_fingerprint(manifest_path),
                "source_spm": file_fingerprint(canonical_spm),
                "speedtree_spm": file_fingerprint(speedtree_spm),
                "canonical_material": (
                    bark_normalization_manifest.get("normalization") or {}
                ).get("canonical_material"),
                "production_source_mutated": False,
            }
        material_preflight = None
        if args.material_contract:
            # Validate the exact SPM/STMAT hashes before Blender or the add-on
            # can mutate a scene.  The add-on still receives the same report
            # path for its existing texture-binding loader.
            material_preflight = validate_preflight_report(
                args.material_contract,
                speedtree_spm,
                require_ok=True,
            )
            report["speedtree_pipeline_contract"] = material_preflight[
                "speedtree_pipeline_contract"
            ]
            report["speedtree_pipeline_contract_required"] = True
        record_stage_duration(report, "input_preflight", preflight_started)
        addon_runtime_started = perf_counter()
        addon_runtime = prepare_runtime(
            "sk_batch.jobs.assembly_headless_job",
            {
                "speedtree_bone_weight_repair": (
                    "speedtree_export_v1",
                    "assembly_pipeline_v1",
                    "atlas_manifest_consumer_v1",
                ),
            },
        )
        report["blender_addon_runtime"] = addon_runtime.receipt
        record_stage_duration(
            report,
            "addon_runtime_prepare",
            addon_runtime_started,
        )
        run_speedtree_cli_export = addon_runtime.operation(
            "speedtree_bone_weight_repair",
            "run_speedtree_cli_export",
        )
        run_import_and_assemble = addon_runtime.operation(
            "speedtree_bone_weight_repair",
            "run_import_and_assemble",
        )

        blend_path = os.path.abspath(args.blend)
        blend_exists = os.path.exists(blend_path)
        blend_open_started = perf_counter()
        if blend_exists:
            bpy.ops.wm.open_mainfile(filepath=blend_path)
        else:
            bpy.ops.wm.read_homefile(use_empty=True)
        record_stage_duration(report, "blend_open", blend_open_started)
        is_cluster_source = is_cluster_normalization_spm(canonical_spm)
        if args.cluster_source_build_only and not is_cluster_source:
            raise RuntimeError(
                "--cluster-source-build-only is valid only for a canonical "
                "Cluster SPM"
            )

        cluster_assembly_handoff = None
        cluster_assembly_handoff_summary = None
        cluster_assembly_source_resolution = None
        if args.cluster_source_build_only:
            # This SPM is a raw provider consumed by another owner's
            # Normalizer transaction. It cannot own a content-driven Assembly
            # receipt under its own path, so scanning historical receipt
            # candidates is pure delay and previously cost 10-20 seconds.
            cluster_receipt_path = None
            cluster_receipt_resolution = {
                "policy": "cluster_source_provider_no_owner_receipt",
                "requested_spm": str(speedtree_spm),
                "selected_receipt": None,
                "current_candidates": [],
                "superseded_current_receipts": [],
                "ignored_stale_candidates": [],
            }
        else:
            cluster_receipt_path, cluster_receipt_resolution = (
                resolve_cluster_receipt_path(
                    speedtree_spm,
                    args.material_contract or None,
                    include_resolution=True,
                )
            )
        report["cluster_assembly_receipt_resolution"] = (
            cluster_receipt_resolution
        )
        cluster_assembly_contract = (
            cluster_assembly_contract_from_material_contract(
                cluster_receipt_path, speedtree_spm
            )
            if cluster_receipt_path
            else None
        )
        if cluster_assembly_contract is not None:
            report["cluster_assembly_receipt"] = file_fingerprint(
                cluster_receipt_path
            )
            cluster_assembly_source_resolution = assembly_source_fbx_resolution(
                cluster_assembly_contract,
                speedtree_spm,
            )
            report["cluster_assembly_source_resolution"] = (
                cluster_assembly_source_resolution
            )

        # This is the installed add-on's stable Blender RNA property name.
        settings = bpy.context.scene.speedtree_bwr_settings
        settings.spm_path = str(speedtree_spm)
        collision_cli, collision_hook = resolve_speedtree_collision_cli()
        settings.speedtree_exe_path = str(collision_cli)
        report["speedtree_collision_cli"] = {
            "status": "required",
            "executable": file_fingerprint(collision_cli),
            "hook": file_fingerprint(collision_hook),
            "policy": (
                "native_cli_collision_high_shade_pruning_then_bundled_fbx_xml"
            ),
        }
        settings.texture_contract_path = (
            os.path.abspath(args.material_contract)
            if args.material_contract
            else ""
        )
        # The batch owns only the immutable category assignment. Numeric
        # response values are shared per preset and edited centrally in Unreal.
        settings.wind_preset = "WEED" if args.wind == "GRASS" else args.wind
        settings.write_unreal_json = True
        settings.write_dynamic_wind_json = True
        report["cluster_source_contract"] = {
            "requested": is_cluster_source,
            "policy": (
                "native_speedtree_skin_passthrough_export_ownership_only"
                if is_cluster_source
                else "not_applicable"
            ),
        }
        # The additive Assembly stage fingerprints the existing Full SK FBX;
        # keep the established Full export enabled instead of synthesizing
        # a second, differently-configured Full mesh inside the builder.
        settings.export_fbx = True

        # default_paths anchors out_dir/JSON to bpy.data.filepath. open_mainfile
        # already set it for existing blends, so only a fresh file needs the
        # anchor save — re-saving a multi-GB blend right before the export
        # rebuilds it was pure disk churn (2x full writes per Assembly).
        if not blend_exists:
            Path(blend_path).parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=blend_path)

        # The add-on UI operator uses one name_stem for both the SpeedTree
        # export and the assembled Blender output. Cluster pairs deliberately
        # have two identities, so perform those two existing core stages with
        # explicit stems instead of deriving one by removing ``SK_``.
        report["atlas_manifest_resolution"] = (
            install_assembly_atlas_manifest_resolver(
                addon_runtime,
                speedtree_spm,
            )
        )

        export_settings = settings.as_dict()
        speedtree_export_started = perf_counter()
        speedtree_export = reusable_preflight_export_bundle(
            material_preflight,
            speedtree_spm,
        )
        if speedtree_export is None:
            speedtree_export = run_speedtree_cli_export(
                str(speedtree_spm),
                speedtree_exe_path=export_settings["speedtree_exe_path"],
                export_options_path=export_settings[
                    "speedtree_export_options_path"
                ],
                fbx_export_options_path=export_settings[
                    "speedtree_fbx_export_options_path"
                ],
                xml_export_options_path=export_settings[
                    "speedtree_xml_export_options_path"
                ],
                output_root=export_settings["speedtree_output_root"],
                name_stem=speedtree_spm.stem,
                export_fbx=export_settings["speedtree_export_fbx"],
                export_xml=export_settings["speedtree_export_xml"],
            )
            report["speedtree_export_source"] = "export_helper"
        else:
            report["speedtree_export_source"] = "validated_material_preflight"
        record_stage_duration(
            report,
            "speedtree_export_bundle",
            speedtree_export_started,
        )
        fbx_export = speedtree_export["exports"].get("fbx", {})
        xml_export = speedtree_export["exports"].get("xml", {})
        if not fbx_export.get("exists"):
            raise RuntimeError("SpeedTree export produced no FBX to import")
        if bark_normalization_manifest is not None:
            if not xml_export.get("exists"):
                raise RuntimeError(
                    "Cluster canonical bark validation requires the paired "
                    "SpeedTree XML export"
                )
            try:
                report["cluster_bark_export_validation"] = (
                    validate_canonical_bark_export_bundle(
                        fbx_export["path"],
                        str(Path(fbx_export["path"]).with_suffix(".stmat")),
                        xml_export["path"],
                        bark_normalization_manifest.get(
                            "normalization"
                        ) or {},
                    )
                )
            except BarkNormalizationError as exc:
                raise RuntimeError(
                    "Cluster canonical bark export validation failed: "
                    + str(exc)
                ) from exc

        # A content-driven Assembly receipt can legitimately be written before
        # its authoritative general-tree FBX exists.  Do not silently degrade
        # that Tree to a Full-SK-only import: export the exact source SPM named
        # by the receipt, then reconcile the actual FBX before assembly continues.
        if (
            cluster_assembly_contract is not None
            and cluster_assembly_handoff is None
            and cluster_assembly_source_resolution.get("status")
            == "legacy_pass_through"
            and cluster_assembly_source_resolution.get("reason")
            == "assembly_source_fbx_pending_export"
        ):
            source_spm_value = cluster_assembly_source_resolution.get(
                "source_spm"
            )
            expected_fbx_value = cluster_assembly_source_resolution.get(
                "source_fbx"
            )
            if not source_spm_value or not expected_fbx_value:
                raise RuntimeError(
                    "Cluster Assembly receipt cannot resolve one authoritative "
                    "source SPM/FBX pair"
                )
            source_spm_path = Path(source_spm_value).resolve()
            expected_fbx_path = Path(expected_fbx_value).resolve()
            if not source_spm_path.is_file():
                raise RuntimeError(
                    "Cluster Assembly authoritative source SPM is missing: "
                    f"{source_spm_path}"
                )

            if source_spm_path == speedtree_spm:
                assembly_source_export = speedtree_export
            else:
                assembly_source_export = run_speedtree_cli_export(
                    str(source_spm_path),
                    speedtree_exe_path=export_settings[
                        "speedtree_exe_path"
                    ],
                    export_options_path=export_settings[
                        "speedtree_export_options_path"
                    ],
                    fbx_export_options_path=export_settings[
                        "speedtree_fbx_export_options_path"
                    ],
                    xml_export_options_path=export_settings[
                        "speedtree_xml_export_options_path"
                    ],
                    output_root=export_settings["speedtree_output_root"],
                    name_stem=source_spm_path.stem,
                    export_fbx=export_settings["speedtree_export_fbx"],
                    export_xml=export_settings["speedtree_export_xml"],
                    # This secondary SPM contributes Assembly geometry through
                    # the same exact native FBX/XML serialization contract.
                )
            report["cluster_assembly_source_export"] = assembly_source_export
            assembly_fbx_export = (
                assembly_source_export.get("exports", {}).get("fbx", {})
            )
            actual_fbx_value = assembly_fbx_export.get("path")
            if not assembly_fbx_export.get("exists") or not actual_fbx_value:
                raise RuntimeError(
                    "Cluster Assembly authoritative SpeedTree export produced "
                    "no FBX"
                )
            actual_fbx_path = Path(actual_fbx_value).resolve()
            if actual_fbx_path != expected_fbx_path:
                raise RuntimeError(
                    "Cluster Assembly authoritative SpeedTree export path "
                    f"mismatch: expected {expected_fbx_path}, got "
                    f"{actual_fbx_path}"
                )

            cluster_assembly_source_resolution = (
                assembly_source_fbx_resolution(
                    cluster_assembly_contract,
                    speedtree_spm,
                )
            )
            report["cluster_assembly_source_resolution"] = (
                cluster_assembly_source_resolution
            )
            if cluster_assembly_source_resolution.get("status") != "ready":
                raise RuntimeError(
                    "Cluster Assembly source FBX remained unavailable after "
                    "the authoritative SpeedTree export"
                )

        # Bind the runtime material input to the exact producer output. The
        # preflight immediately before Blender already produced the same
        # collision/pruning-aware FBX+STMAT bundle in the common cache-hit
        # case, so reuse its content-addressed contract instead of parsing the
        # unchanged SPM three more times. Metadata drift falls back to the
        # existing full refresh and never becomes a separate gate.
        if args.material_contract:
            material_contract_refresh_started = perf_counter()
            reused_material_preflight = (
                reuse_preflight_report_after_unchanged_export(
                    material_preflight,
                    fbx_export,
                )
            )
            if reused_material_preflight is not None:
                material_preflight = reused_material_preflight
            else:
                live_stmat_path = Path(fbx_export["path"]).with_suffix(
                    ".stmat"
                )
                material_preflight = (
                    refresh_preflight_report_after_exact_export(
                        material_preflight,
                        speedtree_spm,
                        live_stmat_path,
                    )
                )
            record_stage_duration(
                report,
                "material_contract_refresh",
                material_contract_refresh_started,
            )
            live_material_contract_path = Path(args.report).with_name(
                Path(args.report).stem + "_live_material_contract.json"
            )
            write_report(live_material_contract_path, material_preflight)
            settings.texture_contract_path = str(live_material_contract_path)
            report["speedtree_pipeline_contract"] = material_preflight[
                "speedtree_pipeline_contract"
            ]
            report["exact_target_export_contract_refresh"] = (
                material_preflight["exact_target_export_contract_refresh"]
            )
            report["live_material_contract"] = source_identity(
                live_material_contract_path
            )
            report["speedtree_pipeline_contract_refreshed_after_export"] = (
                material_preflight["exact_target_export_contract_refresh"]
                .get("status")
                == "refreshed"
            )

        if (
            cluster_assembly_contract is not None
            and cluster_assembly_source_resolution.get("status") == "ready"
        ):
            source_fbx_value = cluster_assembly_source_resolution.get(
                "source_fbx"
            )
            if not source_fbx_value:
                raise RuntimeError(
                    "Cluster Assembly ready source resolution has no FBX path"
                )
            cluster_assembly_handoff = inspect_cluster_assembly_fbx(
                cluster_receipt_path,
                speedtree_spm,
                Path(source_fbx_value),
            )
            cluster_assembly_handoff_summary = (
                compact_cluster_assembly_handoff(cluster_assembly_handoff)
            )
            report["cluster_assembly_handoff"] = (
                cluster_assembly_handoff_summary
            )
            require_cluster_assembly_handoff_ready(
                cluster_assembly_handoff
            )
            report["cluster_assembly_handoff_revalidated_after_export"] = True

        settings.source_fbx_path = fbx_export["path"]
        if xml_export.get("exists"):
            settings.xml_path = xml_export["path"]
        settings.name_stem = canonical_spm.stem
        assembly_settings = settings.as_dict()
        assembly_settings["cluster_source_contract"] = is_cluster_source
        # This job appends the final handoff/Assembly summary and writes the
        # authoritative report once.  The add-on must not write an incomplete
        # intermediate copy immediately before that final write.
        assembly_settings["defer_pipeline_report_write"] = True
        # A Cluster source is parked outside Send2UE only when this invocation
        # is the raw producer for the downstream Cluster Normalizer. Standalone
        # Cluster assets have no later stage that can create ordinal Export
        # pivots, so they must build the normal final Export structure here.
        assembly_settings["defer_cluster_export_to_normalizer"] = bool(
            args.cluster_source_build_only
        )
        assembly_settings["source_identity_path"] = str(canonical_spm)
        canonical_source_fbx = (
            canonical_spm.parent
            / "fbx"
            / f"{canonical_spm.stem}.fbx"
        ).resolve()
        if canonical_source_fbx != Path(fbx_export["path"]).resolve():
            assembly_settings["source_fbx_cleanup_aliases"] = [
                str(canonical_source_fbx)
            ]
        cluster_export_snapshot = None
        if is_cluster_source and not args.cluster_source_build_only:
            cluster_export_snapshot = capture_cluster_export_snapshot(
                bpy.data,
                canonical_spm.stem,
            )
        blender_assembly_started = perf_counter()
        result = run_import_and_assemble(assembly_settings)
        record_stage_duration(
            report,
            "blender_import_and_assemble",
            blender_assembly_started,
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                "SpeedTree Assembly did not return a pipeline report"
            )
        empty_after_dummy_cleanup = bool(
            result.get("status") == "empty_after_dummy_cleanup"
            and (result.get("empty_asset_disposition") or {}).get("reason")
            == "all_renderable_geometry_removed_as_authorized_dummy"
            and (result.get("unassigned_geometry_cleanup") or {}).get(
                "cleanup_authorized"
            )
            is True
            and int(
                (result.get("unassigned_geometry_cleanup") or {}).get(
                    "removed_face_count"
                )
                or 0
            )
            > 0
            and (result.get("renderable_geometry_after_cleanup") or {}).get(
                "status"
            )
            == "empty"
        )
        if empty_after_dummy_cleanup:
            result["native_skin_passthrough"] = False
            report["empty_asset_disposition"] = result[
                "empty_asset_disposition"
            ]
        else:
            native_skin_steps = [
                step
                for step in result.get("steps", [])
                if isinstance(step, dict) and step.get("name") == "merge_export"
            ]
            if (
                len(native_skin_steps) != 1
                or native_skin_steps[0].get("native_skin_passthrough") is not True
            ):
                raise RuntimeError(
                    "SpeedTree Assembly did not prove native skin passthrough"
                )
            result["native_skin_passthrough"] = True
        result["assembly_output_contract_version"] = (
            ASSEMBLY_OUTPUT_CONTRACT_VERSION
        )
        unassigned_geometry_cleanup = result.get(
            "unassigned_geometry_cleanup"
        )
        if not isinstance(unassigned_geometry_cleanup, dict):
            unassigned_geometry_cleanup = {
                "status": "diagnostic_only",
                "policy": "completed_assembly_is_authoritative_v1",
                "telemetry_present": False,
                "cleanup_applied": None,
                "message": (
                    "The active Blender add-on did not emit optional "
                    "unassigned-geometry cleanup telemetry."
                ),
            }
        report["assembly_output_contract_version"] = (
            ASSEMBLY_OUTPUT_CONTRACT_VERSION
        )
        report["unassigned_geometry_cleanup"] = (
            unassigned_geometry_cleanup
        )
        if unassigned_geometry_cleanup.get("status") == "diagnostic_only":
            report.setdefault("warnings", []).append(
                unassigned_geometry_cleanup["message"]
            )
        report["speedtree_export"] = speedtree_export
        transient_export_reconciliation = None
        if cluster_export_snapshot is not None:
            transient_export_reconciliation = (
                reconcile_transient_cluster_export_root(
                    bpy.data,
                    bpy.data.collections.get(
                        assembly_settings.get(
                            "source_collection_name",
                            "SpeedTree_Source",
                        )
                    ),
                    cluster_source_stem=canonical_spm.stem,
                    source_fbx_path=fbx_export["path"],
                    source_identity_path=canonical_spm,
                    before_snapshot=cluster_export_snapshot,
                )
            )
            report["cluster_transient_export_reconciliation"] = (
                transient_export_reconciliation
            )
        export_collection_issues = list(
            (transient_export_reconciliation or {}).get("issues") or ()
        )
        export_collection_issues.extend(
            export_collection_contract_issues(
                canonical_spm.stem if is_cluster_source else ""
            )
        )
        export_collection_issues = list(
            dict.fromkeys(export_collection_issues)
        )
        report["export_collection_issues"] = export_collection_issues
        cluster_export_pending = bool(
            args.cluster_source_build_only
            and is_cluster_source
            and export_collection_issues
        )
        export_collection_diagnostics = (
            [] if cluster_export_pending else export_collection_issues
        )

        stem = canonical_spm.stem
        blend_dir = str(Path(blend_path).parent)
        json_dir = os.path.join(blend_dir, "JSON")
        report.update(
            {
                "status": "ok",
                "name_stem": stem,
                "megaplant_json": os.path.join(json_dir, f"{stem}_megaplant_tree_groups.json"),
                "dynamic_wind_json": os.path.join(
                    json_dir, f"{stem}_dynamic_wind_import_from_megaplant_groups.json"
                ),
                "pipeline_report": os.path.join(
                    blend_dir, "reports", f"{stem}_speedtree_assembly_pipeline_report_codex.json"
                ),
            }
        )
        pipeline_path = Path(report["pipeline_report"])
        # run_import_and_assemble already returns the authoritative pipeline
        # payload.  Keep it in memory so the batch job can append its final
        # handoff data and write the report once, without requiring an
        # incomplete intermediate report on disk.
        pipeline_data = result if isinstance(result, dict) else None
        merged_object = None
        texture_normalization = {}
        empty_material_slots = []
        vertex_color_contract = {
            "status": "diagnostic_not_run",
            "issues": [],
        }
        vertex_payload_contract = {
            "status": "diagnostic_not_run",
            "issues": ["missing_pipeline_report"],
        }
        post_assembly_spm_contract_started = perf_counter()
        reusable_contracts = reusable_preflight_spm_contracts(
            material_preflight,
            fbx_export,
        )
        if reusable_contracts is None:
            leaf_reference_contract = {
                "status": "not_applicable",
            }
            material_export_contract = {
                "status": "not_applicable",
            }
            report["spm_contract_inspection_source"] = "native_export_receipt"
        else:
            leaf_reference_contract, material_export_contract = (
                reusable_contracts
            )
            report["spm_contract_inspection_source"] = (
                "validated_preflight_exact_export"
            )
        record_stage_duration(
            report,
            "post_assembly_spm_contracts",
            post_assembly_spm_contract_started,
        )
        report["leaf_reference_contract"] = leaf_reference_contract
        report["material_export_contract"] = material_export_contract
        leaf_reference_blocked = leaf_reference_contract.get("status") in {
            "inspection_error", "invalid_references",
        }
        material_export_blocked = material_export_contract.get("status") not in {
            "ok", "not_applicable",
        }
        if pipeline_data is None and pipeline_path.is_file():
            pipeline_data = json.loads(pipeline_path.read_text(encoding="utf-8"))
        if pipeline_data is not None:
            pipeline_data["source_review_policy"] = source_review_policy
            pipeline_data["legacy_cluster_lineage"] = report[
                "legacy_cluster_lineage"
            ]
            pipeline_data["cluster_assembly_receipt_resolution"] = (
                cluster_receipt_resolution
            )
            pipeline_data["cluster_assembly_source_resolution"] = (
                cluster_assembly_source_resolution
            )
            pipeline_data["export_collection_issues"] = (
                export_collection_issues
            )
            if transient_export_reconciliation is not None:
                pipeline_data[
                    "cluster_transient_export_reconciliation"
                ] = transient_export_reconciliation
            if cluster_assembly_handoff is not None:
                pipeline_data["cluster_assembly_handoff"] = (
                    cluster_assembly_handoff_summary
                )
            if material_preflight is not None:
                pipeline_data["speedtree_pipeline_contract"] = (
                    material_preflight["speedtree_pipeline_contract"]
                )
                # Preserve the exact validated Assembly input separately from
                # the canonical production contract. Cluster providers may be
                # assembled from an isolated, bark-normalized SPM whose
                # textures intentionally differ from the untouched authored
                # source. Later normalization stages may refresh the canonical
                # envelope, but Push must consume the same content-addressed
                # material input that built this blend.
                pipeline_data["speedtree_material_handoff_contract"] = (
                    material_preflight["speedtree_pipeline_contract"]
                )
                pipeline_data["speedtree_pipeline_contract_required"] = True
            texture_normalization = pipeline_data.get("texture_normalization") or {}
            report["texture_normalization"] = texture_normalization
            report["texture_readiness_contract"] = {
                "status": texture_normalization.get("texture_contract_status", ""),
                "path": texture_normalization.get("texture_contract_path", ""),
            }
            merged_name = str((pipeline_data.get("paths") or {}).get("merged_name") or "")
            merged_object = bpy.data.objects.get(merged_name)
            removed_empty_slots = remove_unused_empty_material_slots(merged_object)
            report["removed_unused_empty_material_slots"] = removed_empty_slots
            vertex_payload_started = perf_counter()
            vertex_payload_contract = pack_speedtree_vertex_payload(
                merged_object,
                mirror_to_nanite_uv=True,
            )
            report["vertex_payload_contract"] = vertex_payload_contract
            vertex_color_contract = {
                "status": "covered_by_vertex_payload_transform",
                "issues": [],
            }
            record_stage_duration(
                report,
                "vertex_payload_finalize",
                vertex_payload_started,
            )
            report["vertex_color_contract"] = vertex_color_contract

        # The Assembly result is the save authority. Optional diagnostics
        # must not discard a completed Blender Assembly.
        report["blend_resaved"] = False
        if merged_object is not None or empty_after_dummy_cleanup:
            blend_save_started = perf_counter()
            if is_cluster_source:
                save_result = save_cluster_source_mainfile(
                    bpy,
                    blend_path,
                    report,
                )
            else:
                save_result = bpy.ops.wm.save_as_mainfile(
                    filepath=blend_path
                )
            report["blend_save_operator_result"] = sorted(save_result)
            if "FINISHED" not in save_result:
                raise RuntimeError(
                    "Blender did not commit the assembled source blend: "
                    + ", ".join(sorted(save_result))
                )
            report["blend_resaved"] = True
            record_stage_duration(
                report,
                "blend_save",
                blend_save_started,
            )

        missing_outputs = [] if empty_after_dummy_cleanup else [
            report[key]
            for key in ("megaplant_json", "dynamic_wind_json")
            if not os.path.exists(report[key])
        ]
        for path in missing_outputs:
            report.setdefault("warnings", []).append(f"expected output missing: {path}")

        reviewable_source_issues = bool(
            empty_material_slots
            or material_export_blocked
            or leaf_reference_blocked
            or export_collection_diagnostics
            or missing_outputs
            or vertex_payload_contract.get("status") == "blocked"
        )
        if empty_after_dummy_cleanup:
            handoff_status = "empty_after_dummy_cleanup"
        elif cluster_export_pending:
            handoff_status = "cluster_export_pending"
        else:
            handoff_status = "ok"
        preflight = {
            "status": handoff_status,
            "export_collection_issues": export_collection_issues,
            "empty_material_slots": empty_material_slots,
            "missing_textures": texture_normalization.get("missing", []),
            "missing_outputs": missing_outputs,
            "vertex_color_contract": vertex_color_contract,
            "vertex_payload_contract": vertex_payload_contract,
            "leaf_reference_contract": leaf_reference_contract,
            "material_export": material_export_contract,
            "cluster_assembly_handoff": cluster_assembly_handoff_summary,
            "missing_materials": material_export_contract.get(
                "missing_materials", []
            ),
            "source_review_required": False,
            "diagnostics_present": reviewable_source_issues,
            "unreal_push_ready": handoff_status == "ok",
        }
        source_blend_committed = bool(
            args.cluster_source_build_only
            and report.get("blend_resaved")
            and merged_object is not None
            and Path(blend_path).is_file()
            and Path(bpy.data.filepath).resolve() == Path(blend_path).resolve()
        )
        cluster_source_build_contract = {
            "status": (
                "ready"
                if (
                    args.cluster_source_build_only
                    and source_blend_committed
                )
                else "blocked"
                if args.cluster_source_build_only
                else "not_applicable"
            ),
            "mode": (
                "raw_source_for_cluster_normalizer"
                if args.cluster_source_build_only
                else "final_handoff"
            ),
            "deferred_export_issues": (
                list(export_collection_issues)
                if cluster_export_pending
                else []
            ),
            "final_export_required": cluster_export_pending,
            "post_normalization_handoff_status": (
                "ok"
            ),
            "source_blend_committed": source_blend_committed,
            "source_object": (
                merged_object.name if merged_object is not None else ""
            ),
        }
        report["cluster_source_build_contract"] = (
            cluster_source_build_contract
        )
        report["handoff_preflight"] = preflight
        report["source_review_required"] = False
        report["unreal_push_ready"] = handoff_status == "ok"
        assembly_manifest = None
        assembly_manifest_summary = None
        current_handoff = None
        if (
            preflight["status"] == "ok"
            and (
                not isinstance(cluster_assembly_handoff, dict)
                or cluster_assembly_handoff.get("status")
                not in {"ready", "pass_through"}
            )
            and pipeline_data is not None
            and merged_object is not None
        ):
            current_full_fbx = str(
                (pipeline_data.get("paths") or {}).get("fbx") or ""
            )
            current_handoff = current_assembly_manifest_handoff(
                canonical_spm,
                current_full_fbx,
            )
            if current_handoff is not None:
                report["cluster_assembly_current_manifest_authority"] = (
                    current_handoff["current_manifest_authority"]
                )
        assembly_mode, selected_assembly_handoff = (
            select_cluster_assembly_build_handoff(
                cluster_assembly_contract,
                cluster_assembly_handoff,
                current_handoff,
            )
        )
        if preflight["status"] == "ok" and assembly_mode == "pass_through":
            # Persist "no content-driven Assembly" as a positive current
            # contract. Without this manifest, Push falls back to historical
            # target registries and can falsely dependency-orchestrate an
            # otherwise ordinary Full-SK asset.
            assembly_started = perf_counter()
            assembly_manifest = build_blender_assembly_inputs(
                selected_assembly_handoff,
                None,
                None,
                Path(blend_dir) / "assembly",
                "",
                report["dynamic_wind_json"],
                pass_through_receipt_path=cluster_receipt_path,
                pass_through_target_contract=cluster_assembly_contract,
                pass_through_target_spm=speedtree_spm,
            )
            record_stage_duration(
                report, "cluster_assembly_build", assembly_started
            )
            assembly_manifest_summary = compact_cluster_assembly_manifest(
                assembly_manifest
            )
            report["cluster_assembly_manifest"] = assembly_manifest_summary
        elif (
            preflight["status"] == "ok"
            and assembly_mode == "build"
        ):
            if pipeline_data is None or merged_object is None:
                raise RuntimeError(
                    "Cluster Assembly builder requires the final pipeline mesh"
                )
            final_armature = merged_object.find_armature()
            if final_armature is None:
                raise RuntimeError(
                    "Cluster Assembly builder could not resolve the final armature"
                )
            full_fbx_path = str((pipeline_data.get("paths") or {}).get("fbx") or "")
            if not full_fbx_path:
                raise RuntimeError(
                    "Cluster Assembly builder found no final Full SK FBX path"
                )
            assembly_started = perf_counter()
            assembly_manifest = build_blender_assembly_inputs(
                selected_assembly_handoff,
                final_armature,
                merged_object,
                Path(blend_dir) / "assembly",
                full_fbx_path,
                report["dynamic_wind_json"],
                target_native_receipt_path=(
                    speedtree_export.get("native_receipt")
                    or fbx_export.get("native_receipt")
                ),
            )
            record_stage_duration(
                report, "cluster_assembly_build", assembly_started
            )
            assembly_manifest_summary = compact_cluster_assembly_manifest(
                assembly_manifest
            )
            report["cluster_assembly_manifest"] = assembly_manifest_summary
        if (
            bark_normalization_manifest is not None
            and preflight["status"] != "blocked"
        ):
            canonical_xml = (
                canonical_spm.parent
                / "xml"
                / f"{canonical_spm.stem}.xml"
            )
            atomic_copy(xml_export["path"], canonical_xml)
            report["cluster_bark_canonical_xml_handoff"] = (
                file_fingerprint(canonical_xml)
            )
        if pipeline_data is not None:
            blend_fingerprint_started = perf_counter()
            source_blend_identity = file_fingerprint(
                Path(blend_path),
                hash_content=False,
            )
            source_blend_identity["fingerprint_policy"] = (
                "path_size_mtime_v1"
            )
            pipeline_data["source_blend_identity"] = source_blend_identity
            record_stage_duration(
                report,
                "blend_identity_fingerprint",
                blend_fingerprint_started,
            )
            pipeline_data["speedtree_live_source_identity"] = {
                "spm": source_identity(canonical_spm),
            }
            if bark_normalization_manifest is not None:
                pipeline_data["cluster_bark_source_resolution"] = report[
                    "cluster_bark_source_resolution"
                ]
                pipeline_data["cluster_bark_export_validation"] = report[
                    "cluster_bark_export_validation"
                ]
                pipeline_data[
                    "cluster_bark_canonical_xml_handoff"
                ] = report["cluster_bark_canonical_xml_handoff"]
            pipeline_data["handoff_preflight"] = preflight
            pipeline_data["cluster_source_build_contract"] = (
                cluster_source_build_contract
            )
            if report.get("blend_save_policy") is not None:
                pipeline_data["blend_save_policy"] = report[
                    "blend_save_policy"
                ]
            if assembly_manifest is not None:
                pipeline_data["cluster_assembly_manifest"] = (
                    assembly_manifest_summary
                )
            if preflight["status"] == "ok":
                export_postcondition_started = perf_counter()
                pipeline_data["assembly_export_postcondition"] = (
                    export_object_postcondition(bpy.data)
                )
                record_stage_duration(
                    report,
                    "export_object_postcondition",
                    export_postcondition_started,
                )
            pipeline_report_started = perf_counter()
            write_report(pipeline_path, pipeline_data)
            record_stage_duration(
                report,
                "pipeline_report_write",
                pipeline_report_started,
            )
    except Exception as exc:
        mark_job_failed(report, exc, traceback.format_exc())
    record_stage_duration(report, "total_job", job_started)
    write_report(args.report, report)
    if report["status"] != "ok":
        sys.exit(1)


main()
