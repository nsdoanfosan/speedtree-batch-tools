"""Headless Blender job: SPM -> SpeedTree CLI export -> BWR import/repair -> .blend.

Run:
  blender.exe -b --python bwr_headless_job.py -- --spm X.spm --blend X.blend
      --wind TREE|BUSH|WEED|NONE --material-contract preflight.json
      --report result.json

Runs with --factory-startup and enables only the junction-installed
speedtree_bone_weight_repair add-on for this process.  This avoids loading every
interactive user add-on in a background batch. Re-running on an existing .blend
is a clean idempotent update (the operator wipes its previous build).
"""
import argparse
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

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

from vertex_color_contract import (
    inspect_object_vertex_colors,
    pack_speedtree_vertex_payload,
)
from repair_push_evidence import export_object_postcondition
from spm_leaf_handoff_contract import (
    inspect_speedtree_material_export,
    inspect_spm_leaf_contract,
    leaf_contract_user_message,
)
from speedtree_pipeline_contract import (
    refresh_preflight_report_after_exact_export,
    source_identity,
    validate_preflight_report,
)
from bwr_atlas_manifest_bridge import install_bwr_atlas_manifest_resolver
from cluster_assembly_handoff_contract import (
    assembly_source_fbx_resolution,
    build_assembly_handoff,
    build_blender_fbx_inventory,
    current_assembly_manifest_repair_handoff,
    file_fingerprint,
    load_cluster_contract,
    resolve_cluster_receipt_path,
    role_identity_aliases_from_contract,
)
from cluster_assembly_builder import build_blender_assembly_inputs
from spm_audit import is_cluster_normalization_spm
from speedtree_legacy_cluster_contract import inspect_legacy_cluster_state
from job_report_contract import mark_job_failed
from repair_runtime_contract import (
    REPAIR_OUTPUT_CONTRACT_VERSION,
    RepairPipelineEvidenceError,
    validate_unassigned_geometry_cleanup_evidence,
)
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


VERTEX_COLOR_ISSUE_TEXT = {
    "green_channel_has_no_signal": (
        "나무 높이 마스크(VertexColor.G)가 전부 0 — SpeedTree의 "
        "Tree/Trunk/Branch Vertex Color Green 설정 확인"
    ),
}


def friendly_vertex_color_issues(report):
    return [
        VERTEX_COLOR_ISSUE_TEXT.get(issue, issue)
        for issue in (report.get("issues") or ["unknown"])
    ]


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
    parser.add_argument("--manual-bones-locked", action="store_true")
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def write_report(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_cluster_source_mainfile(bpy_module, filepath, report):
    """Save one Cluster source without Blender's redundant version rename.

    The GUI owns a separate pre-repair copy and restores it if this producer
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
            "gui_pre_repair_transaction_authoritative_disable_blender_"
            "version_backup"
        ),
        "scope": "headless_cluster_source_final_save",
        "preference": "bpy.context.preferences.filepaths.save_version",
        "original_save_version": original_save_version,
        "effective_save_version": 0,
        "preference_persisted": False,
        "preference_restored": False,
        "transaction_backup": "sk_batch_gui_pre_repair_copy_and_rollback",
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


def material_slot_issues(obj):
    if obj is None:
        return [{"object": "<missing export mesh>", "slot": None, "used_faces": 0}]
    if obj.type != "MESH" or not obj.data:
        return [{"object": obj.name, "slot": None, "used_faces": 0}]
    if len(obj.data.materials) == 0 and len(obj.data.polygons) > 0:
        return [{
            "object": obj.name,
            "slot": 0,
            "used_faces": len(obj.data.polygons),
        }]
    issues = []
    for slot_index, material in enumerate(obj.data.materials):
        if material is not None:
            continue
        issues.append(
            {
                "object": obj.name,
                "slot": slot_index,
                "used_faces": sum(
                    1 for polygon in obj.data.polygons
                    if polygon.material_index == slot_index
                ),
            }
        )
    return issues


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
    """Import the exact FBX in-memory and reconcile it before BWR can save.

    The later BWR operator clears these tagged objects and performs its normal
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
        "PCG Cluster Assembly handoff blocked before Blender Repair: "
        + (reasons or "unknown contract error")
    )


def select_cluster_assembly_build_handoff(receipt_contract, inspected_handoff):
    """Prefer current FBX evidence over a stale pass-through receipt.

    The PCG receipt describes what was known before the final BWR FBX existed.
    Once that FBX has been inspected, a ready handoff is the authoritative
    content signal and must create Assembly inputs even when the older receipt
    recorded ``pass_through``.
    """
    if (
        isinstance(inspected_handoff, dict)
        and inspected_handoff.get("status") == "ready"
    ):
        return "build", inspected_handoff

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
    args = parse_args()
    canonical_spm = Path(args.spm).resolve()
    speedtree_spm = Path(args.speedtree_spm or args.spm).resolve()
    report = {
        "spm": str(canonical_spm),
        "speedtree_spm": str(speedtree_spm),
        "blend": args.blend,
        "wind": args.wind,
        "manual_bones_locked": bool(args.manual_bones_locked),
        "status": "failed",
    }
    # Cluster rows reach this job under their canonical SK_ output identity.
    # ``--speedtree-spm`` can additionally point at an immutable isolated copy
    # when the Assembly receipt requires canonical bark before Atlas capture.
    # Legacy receipt lineage is diagnostic only and never relaxes validation.
    legacy_state = inspect_legacy_cluster_state(speedtree_spm)
    legacy_cluster_origin = bool(
        legacy_state.get("receipt_valid")
        and legacy_state.get("classified_generator_guids")
    )
    source_review_allowed = False
    source_review_policy = "strict"
    report["source_review_policy"] = source_review_policy
    report["legacy_cluster_lineage"] = {
        "status": "recognized" if legacy_cluster_origin else "not_applicable",
        "receipt": legacy_state.get("receipt", ""),
        "receipt_valid": bool(legacy_state.get("receipt_valid")),
        "generator_count": len(
            legacy_state.get("classified_generator_guids") or []
        ),
        "generator_guids": legacy_state.get("classified_generator_guids") or [],
        "marker_drift_guids": legacy_state.get("marker_drift_guids") or [],
        "errors": legacy_state.get("errors") or [],
    }
    if report["legacy_cluster_lineage"]["marker_drift_guids"]:
        report.setdefault("warnings", []).append(
            "Legacy Cluster foreground marker drift is diagnostic only; "
            "the GUID receipt does not relax source validation."
        )
    try:
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
        addon_runtime = prepare_runtime(
            "sk_batch.jobs.bwr_headless_job",
            {
                "speedtree_bone_weight_repair": (
                    "spm_sk_preflight_v1",
                    "speedtree_export_v1",
                    "repair_pipeline_v1",
                    "atlas_manifest_consumer_v1",
                ),
            },
        )
        report["blender_addon_runtime"] = addon_runtime.receipt
        require_spm_sk_ready = addon_runtime.operation(
            "speedtree_bone_weight_repair",
            "require_spm_sk_ready",
        )
        run_speedtree_cli_export = addon_runtime.operation(
            "speedtree_bone_weight_repair",
            "run_speedtree_cli_export",
        )
        run_import_and_repair = addon_runtime.operation(
            "speedtree_bone_weight_repair",
            "run_import_and_repair",
        )

        # Reject authored-but-disabled Branch skeletons before creating an
        # empty .blend. BranchMesh-only assets remain valid and use the rigid
        # one-bone fallback inside the add-on.
        if not args.manual_bones_locked:
            require_spm_sk_ready(str(speedtree_spm))

        blend_path = os.path.abspath(args.blend)
        blend_exists = os.path.exists(blend_path)
        if blend_exists:
            bpy.ops.wm.open_mainfile(filepath=blend_path)
        else:
            bpy.ops.wm.read_homefile(use_empty=True)

        cluster_assembly_handoff = None
        cluster_assembly_source_resolution = None
        cluster_receipt_path, cluster_receipt_resolution = resolve_cluster_receipt_path(
            speedtree_spm,
            args.material_contract or None,
            include_resolution=True,
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

        settings = bpy.context.scene.speedtree_bwr_settings
        settings.spm_path = str(speedtree_spm)
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
        is_cluster_source = is_cluster_normalization_spm(canonical_spm)
        if args.cluster_source_build_only and not is_cluster_source:
            raise RuntimeError(
                "--cluster-source-build-only is valid only for a canonical "
                "Cluster SPM"
            )
        report["cluster_source_skin_contract"] = {
            "requested": is_cluster_source,
            "policy": (
                "canonicalize_xml_render_root_axes_preserve_authored_skin_or_bind_unskinned_single_axis"
                if is_cluster_source
                else "not_applicable"
            ),
        }
        # The additive Assembly stage fingerprints the existing Full SK FBX;
        # keep the established BWR Full export enabled instead of synthesizing
        # a second, differently-configured Full mesh inside the builder.
        settings.export_fbx = True

        # default_paths anchors out_dir/JSON to bpy.data.filepath. open_mainfile
        # already set it for existing blends, so only a fresh file needs the
        # anchor save — re-saving a multi-GB blend right before the export
        # rebuilds it was pure disk churn (2x full writes per repair).
        if not blend_exists:
            Path(blend_path).parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=blend_path)

        # The add-on UI operator uses one name_stem for both the SpeedTree
        # export and the repaired Blender output.  Cluster pairs deliberately
        # have two identities, so perform those two existing core stages with
        # explicit stems instead of deriving one by removing ``SK_``.
        report["atlas_manifest_resolution"] = (
            install_bwr_atlas_manifest_resolver(
                addon_runtime,
                speedtree_spm,
            )
        )

        export_settings = settings.as_dict()
        speedtree_export = run_speedtree_cli_export(
            str(speedtree_spm),
            speedtree_exe_path=export_settings["speedtree_exe_path"],
            export_options_path=export_settings["speedtree_export_options_path"],
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
            # A user-authored manual-bones marker is an explicit preservation
            # contract. Keep the normal FBX preset (including authored bones)
            # while bypassing only the automatic Branch-generator gate.
            allow_manual_bones=bool(args.manual_bones_locked),
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
        # by the receipt, then reconcile the actual FBX before repair continues.
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
                    # This secondary SPM contributes Assembly geometry only.
                    # Its authored Branch bones are not the final SK skeleton,
                    # so export it with the bundled no-bones preset when all
                    # visible Branch generators are Absolute/0.
                    allow_boneless=True,
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

        # SpeedTree FBX export promotes a newly generated STMAT sidecar.  The
        # initial validation above proved the exact SPM/report pair before any
        # mutation; now bind a derived runtime contract to the exact producer
        # output instead of treating that expected replacement as an asset
        # failure.  The original preflight receipt remains immutable.
        if args.material_contract:
            live_stmat_path = Path(fbx_export["path"]).with_suffix(".stmat")
            material_preflight = refresh_preflight_report_after_exact_export(
                material_preflight,
                speedtree_spm,
                live_stmat_path,
            )
            live_material_contract_path = Path(args.report).with_name(
                Path(args.report).stem + "_live_material_contract.json"
            )
            write_report(live_material_contract_path, material_preflight)
            validate_preflight_report(
                live_material_contract_path,
                speedtree_spm,
                require_ok=True,
            )
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
            report["speedtree_pipeline_contract_revalidated_after_export"] = True

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
            report["cluster_assembly_handoff"] = cluster_assembly_handoff
            require_cluster_assembly_handoff_ready(
                cluster_assembly_handoff
            )
            report["cluster_assembly_handoff_revalidated_after_export"] = True

        settings.source_fbx_path = fbx_export["path"]
        if xml_export.get("exists"):
            settings.xml_path = xml_export["path"]
        settings.name_stem = canonical_spm.stem
        repair_settings = settings.as_dict()
        repair_settings["cluster_source_skin_contract"] = is_cluster_source
        # A Cluster source is parked outside Send2UE only when this invocation
        # is the raw producer for the downstream Cluster Normalizer. Standalone
        # Cluster assets have no later stage that can create ordinal Export
        # pivots, so they must build the normal final Export structure here.
        repair_settings["defer_cluster_export_to_normalizer"] = bool(
            args.cluster_source_build_only
        )
        repair_settings["source_identity_path"] = str(canonical_spm)
        canonical_source_fbx = (
            canonical_spm.parent
            / "fbx"
            / f"{canonical_spm.stem}.fbx"
        ).resolve()
        if canonical_source_fbx != Path(fbx_export["path"]).resolve():
            repair_settings["source_fbx_cleanup_aliases"] = [
                str(canonical_source_fbx)
            ]
        cluster_export_snapshot = None
        if is_cluster_source and not args.cluster_source_build_only:
            cluster_export_snapshot = capture_cluster_export_snapshot(
                bpy.data,
                canonical_spm.stem,
            )
        result = run_import_and_repair(repair_settings)
        try:
            unassigned_geometry_cleanup = (
                validate_unassigned_geometry_cleanup_evidence(
                    result,
                    expected_spm=canonical_spm,
                    expected_fbx=fbx_export["path"],
                    require_recheck=True,
                    missing_is_diagnostic=True,
                )
            )
        except RepairPipelineEvidenceError as exc:
            raise RuntimeError(
                "Blender Repair output contract requires pre-repair "
                "Default/empty-material geometry cleanup evidence: "
                f"{exc}"
            ) from exc
        report["repair_output_contract_version"] = (
            REPAIR_OUTPUT_CONTRACT_VERSION
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
                        repair_settings.get(
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
        blocking_export_collection_issues = (
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
                    blend_dir, "reports", f"{stem}_speedtree_repair_pipeline_report_codex.json"
                ),
            }
        )
        pipeline_path = Path(report["pipeline_report"])
        pipeline_data = None
        texture_normalization = {}
        empty_material_slots = []
        vertex_color_contract = {
            "status": "blocked",
            "issues": ["missing_pipeline_report"],
        }
        vertex_payload_contract = {
            "status": "blocked",
            "issues": ["missing_pipeline_report"],
        }
        leaf_reference_contract = inspect_spm_leaf_contract(speedtree_spm)
        material_export_contract = inspect_speedtree_material_export(
            speedtree_spm, leaf_reference_contract
        )
        report["leaf_reference_contract"] = leaf_reference_contract
        report["material_export_contract"] = material_export_contract
        leaf_reference_blocked = leaf_reference_contract.get("status") in {
            "inspection_error", "invalid_references", "replacement_needed",
        }
        material_export_blocked = material_export_contract.get("status") not in {
            "ok", "not_applicable",
        }
        if pipeline_path.is_file():
            pipeline_data = json.loads(pipeline_path.read_text(encoding="utf-8"))
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
                    cluster_assembly_handoff
                )
            if material_preflight is not None:
                pipeline_data["speedtree_pipeline_contract"] = (
                    material_preflight["speedtree_pipeline_contract"]
                )
                # Preserve the exact validated Repair input separately from
                # the canonical production contract. Cluster providers may be
                # repaired from an isolated, bark-normalized SPM whose
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
            empty_material_slots = material_slot_issues(merged_object)
            vertex_payload_contract = pack_speedtree_vertex_payload(
                merged_object,
                mirror_to_nanite_uv=True,
            )
            report["vertex_payload_contract"] = vertex_payload_contract
            vertex_color_contract = inspect_object_vertex_colors(
                merged_object,
                require_green_signal=args.wind == "TREE",
            )
            report["vertex_color_contract"] = vertex_color_contract
            if "green_channel_has_no_signal" in vertex_color_contract.get("warnings", []):
                report.setdefault("warnings", []).append(
                    "VertexColor.G height mask is all zero; height attenuation "
                    "will be disabled, but RGB/AO/UV payload is valid."
                )
            if "green_channel_sparse_by_contract" in vertex_color_contract.get("warnings", []):
                green = (vertex_color_contract.get("channels") or {}).get("g") or {}
                report.setdefault("warnings", []).append(
                    "VertexColor.G is sparse by contract: "
                    f"zero={green.get('zero_ratio', 0.0):.2%}, "
                    f"mean={green.get('mean', 0.0):.6f}, "
                    f"max={green.get('max', 0.0):.6f}"
                )

        # Persist the rebuilt scene only after the AO/UV payload is complete.
        # A blocked payload leaves an existing source .blend untouched.
        report["blend_resaved"] = False
        if (
            not blocking_export_collection_issues
            and vertex_payload_contract.get("status") == "ok"
            and vertex_color_contract.get("status") == "ok"
            and not leaf_reference_blocked
            and (
                source_review_allowed
                or (not empty_material_slots and not material_export_blocked)
            )
        ):
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
                    "Blender did not commit the repaired source blend: "
                    + ", ".join(sorted(save_result))
                )
            report["blend_resaved"] = True

        missing_outputs = [
            report[key]
            for key in ("megaplant_json", "dynamic_wind_json", "pipeline_report")
            if not os.path.exists(report[key])
        ]
        for path in missing_outputs:
            report.setdefault("warnings", []).append(f"expected output missing: {path}")

        reviewable_source_issues = bool(
            empty_material_slots
            or material_export_blocked
        )
        structural_handoff_blocked = bool(
            blocking_export_collection_issues
            or missing_outputs
            or vertex_color_contract.get("status") == "blocked"
            or vertex_payload_contract.get("status") == "blocked"
            or leaf_reference_blocked
        )
        hard_handoff_blocked = bool(
            structural_handoff_blocked
            or (reviewable_source_issues and not source_review_allowed)
        )
        if hard_handoff_blocked:
            handoff_status = "blocked"
        elif cluster_export_pending:
            handoff_status = "cluster_export_pending"
        elif reviewable_source_issues:
            handoff_status = "source_review"
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
            "cluster_assembly_handoff": cluster_assembly_handoff,
            "missing_materials": material_export_contract.get(
                "missing_materials", []
            ),
            "source_review_required": handoff_status == "source_review",
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
                    and not hard_handoff_blocked
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
                "source_review" if reviewable_source_issues else "ok"
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
        report["source_review_required"] = handoff_status == "source_review"
        report["unreal_push_ready"] = handoff_status == "ok"
        assembly_manifest = None
        assembly_mode, selected_assembly_handoff = (
            select_cluster_assembly_build_handoff(
                cluster_assembly_contract,
                cluster_assembly_handoff,
            )
        )
        if (
            preflight["status"] == "ok"
            and assembly_mode is None
            and pipeline_data is not None
            and merged_object is not None
        ):
            current_full_fbx = str(
                (pipeline_data.get("paths") or {}).get("fbx") or ""
            )
            current_handoff = current_assembly_manifest_repair_handoff(
                canonical_spm,
                current_full_fbx,
            )
            if current_handoff is not None:
                assembly_mode = "build"
                selected_assembly_handoff = current_handoff
                report["cluster_assembly_current_manifest_authority"] = (
                    current_handoff["current_manifest_authority"]
                )
        if preflight["status"] == "ok" and assembly_mode == "pass_through":
            # Persist "no content-driven Assembly" as a positive current
            # contract. Without this manifest, Push falls back to historical
            # target registries and can falsely dependency-orchestrate an
            # otherwise ordinary Full-SK asset.
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
            report["cluster_assembly_manifest"] = assembly_manifest
        elif (
            preflight["status"] == "ok"
            and assembly_mode == "build"
        ):
            if pipeline_data is None or merged_object is None:
                raise RuntimeError(
                    "Cluster Assembly builder requires the final BWR pipeline mesh"
                )
            final_armature = merged_object.find_armature()
            if final_armature is None:
                raise RuntimeError(
                    "Cluster Assembly builder could not resolve the final BWR armature"
                )
            full_fbx_path = str((pipeline_data.get("paths") or {}).get("fbx") or "")
            if not full_fbx_path:
                raise RuntimeError(
                    "Cluster Assembly builder found no final Full SK FBX path"
                )
            assembly_manifest = build_blender_assembly_inputs(
                selected_assembly_handoff,
                final_armature,
                merged_object,
                Path(blend_dir) / "assembly",
                full_fbx_path,
                report["dynamic_wind_json"],
            )
            report["cluster_assembly_manifest"] = assembly_manifest
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
            pipeline_data["source_blend_identity"] = file_fingerprint(
                Path(blend_path)
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
                pipeline_data["cluster_assembly_manifest"] = assembly_manifest
            if preflight["status"] in {"ok", "source_review"}:
                pipeline_data["repair_push_export_postcondition"] = (
                    export_object_postcondition(bpy.data)
                )
            write_report(pipeline_path, pipeline_data)
        if preflight["status"] == "blocked":
            reasons = []
            if blocking_export_collection_issues:
                reasons.append(
                    "Send2UE Export 구조 오류: "
                    + ", ".join(blocking_export_collection_issues)
                )
            if empty_material_slots:
                details = ", ".join(
                    f"{item['object']} slot {item['slot']}"
                    for item in empty_material_slots
                )
                reasons.append("머티리얼 빈 슬롯: " + details)
            if missing_outputs:
                reasons.append("핸드오프 파일 누락: " + ", ".join(missing_outputs))
            if vertex_color_contract.get("status") == "blocked":
                reasons.append(
                    "버텍스 컬러 계약 실패: "
                    + ", ".join(friendly_vertex_color_issues(vertex_color_contract))
                )
            if vertex_payload_contract.get("status") == "blocked":
                reasons.append(
                    "SpeedTree AO/Nanite UV payload failed: "
                    + ", ".join(vertex_payload_contract.get("issues") or ["unknown"])
                )
            if leaf_reference_blocked:
                status = leaf_reference_contract.get("status")
                if status == "replacement_needed":
                    _ok, message = leaf_contract_user_message(
                        leaf_reference_contract
                    )
                    reasons.append(message)
                else:
                    reasons.append(
                        "SPM leaf 참조 실패: "
                        + "; ".join(
                            leaf_reference_contract.get("issues")
                            or [leaf_reference_contract.get("error", "unknown")]
                        )
                    )
            if material_export_blocked:
                missing_materials = material_export_contract.get(
                    "missing_materials", []
                )
                if missing_materials:
                    reasons.append(
                        "SpeedTree export 재질 누락: "
                        + ", ".join(missing_materials)
                    )
                else:
                    reasons.append(
                        "SpeedTree .stmat 검사 실패: "
                        + str(material_export_contract.get("status") or "unknown")
                    )
            report["status"] = "blocked"
            report["error"] = "② Blender Repair 사전검사 차단 — " + " | ".join(reasons)
    except Exception as exc:
        mark_job_failed(report, exc, traceback.format_exc())
    write_report(args.report, report)
    if report["status"] != "ok":
        sys.exit(1)


main()
