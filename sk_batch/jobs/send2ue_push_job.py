"""Headless Blender job: push a repaired SK .blend to Unreal via send2ue, then
wire the dynamic-wind JSON into the imported skeletal mesh and save to disk.

Run (Unreal editor must be open):
  blender.exe -b X.blend --python send2ue_push_job.py -- --report result.json
      --spm X.spm --material-contract current_preflight.json

Steps:
  1. verify the send2ue <-> Unreal RPC connection
  2. bpy.ops.wm.send2ue('EXEC_DEFAULT')  (skip_animation_export forced on)
  3. remote-run Unreal python: CodexDynamicWindImportLibrary
     .import_dynamic_wind_json_to_skeletal_mesh(mesh, <stem>_dynamic_wind_...json)
     + save_asset/save_directory (send2ue imports are memory-only otherwise).
     The Unreal side writes a result JSON we read back for ground truth.
"""
import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

# send2ue normally reports connection failures through a Blender popup. In
# background mode popup_menu() can crash Blender before our JSON report is
# written. Development/error mode turns those UI reports into catchable Python
# exceptions instead.
os.environ.setdefault("SEND2UE_DEV", "1")

import bpy

JOBS_DIR = str(Path(__file__).resolve().parent)
if JOBS_DIR not in sys.path:
    sys.path.insert(0, JOBS_DIR)
SK_BATCH_DIR = str(Path(__file__).resolve().parent.parent)
if SK_BATCH_DIR not in sys.path:
    sys.path.insert(0, SK_BATCH_DIR)
REPO_DIR = str(Path(SK_BATCH_DIR).parent)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from vertex_color_contract import (
    inspect_object_vertex_colors,
    pack_speedtree_vertex_payload,
)
from cluster_assembly_builder import (
    build_unreal_ingest_plan,
    file_fingerprint as cluster_file_fingerprint,
    scope_material_pipeline_for_destination,
    validate_file_fingerprint,
    validate_manifest_artifacts,
)
from cluster_spm_pair_contract import (
    ClusterSpmPairPathError,
    resolve_cluster_spm_pair,
)
from dynamic_wind_handoff_policy import (
    CLUSTER_ASSET_ROLE_KEY,
    CLUSTER_GENERATED_FLAG,
    resolve_dynamic_wind_policy,
)
from send2ue_manifest_contract import (
    is_actionable_cluster_assembly_manifest,
    manifest_checkout_asset_paths,
    normalize_manifest_handoff_sidecars,
    primary_mesh_asset_path,
    validate_material_handoff_wrapper,
)
from repair_push_evidence import (
    RepairPushEvidenceError,
    stale_execution_freeze_message,
    validate_export_object_postcondition,
    validate_repair_push_evidence_bundle,
)
from speedtree_pipeline_contract import shared_contract_api
from child_progress_contract import (
    SEND2UE_DISK_EXPORT_DONE_MARKER,
    SEND2UE_DISK_EXPORT_START_MARKER,
    SEND2UE_JOB_DONE_MARKER,
    SEND2UE_JOB_FAILED_MARKER,
    SEND2UE_JOB_START_MARKER,
    SEND2UE_RPC_OWNED_DONE_MARKER,
    SEND2UE_RPC_OWNED_START_MARKER,
    emit_progress_marker as emit_child_progress_marker,
)


def load_cluster_assembly_manifest(blend_dir, spm_path):
    """Load the BWR-produced additive manifest, if this content has one."""
    pipeline_path = (
        Path(blend_dir)
        / "reports"
        / f"{Path(spm_path).stem}_speedtree_repair_pipeline_report_codex.json"
    )
    if not pipeline_path.is_file():
        return None
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    embedded = pipeline.get("cluster_assembly_manifest")
    if not isinstance(embedded, dict):
        return None
    if embedded.get("status") == "pass_through":
        return embedded
    manifest_path = ((embedded.get("manifest") or {}).get("path") or "")
    if not manifest_path or not Path(manifest_path).is_file():
        raise RuntimeError(
            "BWR Cluster Assembly manifest file is missing: " + str(manifest_path)
        )
    validate_file_fingerprint(
        embedded.get("manifest"), "BWR Cluster Assembly manifest"
    )
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    manifest["manifest"] = embedded.get("manifest")
    if manifest.get("kind") != "sk_batch_cluster_nanite_assembly_inputs":
        raise RuntimeError("unsupported BWR Cluster Assembly manifest kind")
    validate_manifest_artifacts(manifest)
    return manifest


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--spm", required=True)
    parser.add_argument("--material-contract", required=True)
    parser.add_argument("--repair-evidence")
    parser.add_argument("--ue-timeout", type=float, default=180.0)
    parser.add_argument("--rpc-timeout-min", type=float, default=180.0)
    parser.add_argument("--rpc-timeout-max", type=float, default=900.0)
    parser.add_argument("--rpc-multicast-bind-address")
    parser.add_argument("--rpc-multicast-group-endpoint")
    parser.add_argument("--rpc-multicast-ttl", type=int)
    parser.add_argument("--max-push-polygons", type=int, default=2_000_000)
    parser.add_argument("--max-push-bones", type=int, default=1_500)
    parser.add_argument("--require-green-signal", action="store_true")
    parser.add_argument("--skip-wind", action="store_true")
    parser.add_argument("--transport", choices=("rpc", "headless_export"), default="rpc")
    parser.add_argument("--manifest")
    parser.add_argument("--checkpoint")
    parser.add_argument("--batch-report")
    parser.add_argument("--export-root")
    parser.add_argument("--queue-id")
    parser.add_argument("--source-fingerprint", default="")
    parser.add_argument("--dependency-orchestrated", action="store_true")
    parser.add_argument("--item-import-report")
    parser.add_argument("--unreal-ingest")
    parser.add_argument("--send2ue-unreal-py")
    return parser.parse_args(argv)


def configure_send2ue_rpc_preferences(args):
    """Apply project-matched RPC settings to this factory-startup session."""
    addon = bpy.context.preferences.addons.get("send2ue")
    if addon is None:
        raise RuntimeError("Send2UE preferences are unavailable after add-on setup")
    preferences = addon.preferences
    before = {
        "command_endpoint": str(preferences.command_endpoint),
        "multicast_group_endpoint": str(preferences.multicast_group_endpoint),
        "multicast_ttl": int(preferences.multicast_ttl),
    }

    bind_address = str(args.rpc_multicast_bind_address or "").strip()
    if bind_address:
        bind_ip = ipaddress.ip_address(bind_address)
        if bind_ip.version != 4 or bind_ip.is_unspecified:
            raise RuntimeError(
                f"invalid Send2UE RPC multicast bind address: {bind_address}"
            )
        _old_host, separator, port_text = str(
            preferences.command_endpoint
        ).rpartition(":")
        if not separator or not port_text.isdigit():
            raise RuntimeError(
                "invalid Send2UE RPC command endpoint: "
                + str(preferences.command_endpoint)
            )
        preferences.command_endpoint = f"{bind_address}:{int(port_text)}"

    group_endpoint = str(args.rpc_multicast_group_endpoint or "").strip()
    if group_endpoint:
        group_match = re.fullmatch(r"([^:\s]+):(\d{1,5})", group_endpoint)
        if (
            not group_match
            or not 0 < int(group_match.group(2)) <= 65535
        ):
            raise RuntimeError(
                f"invalid Send2UE RPC multicast group endpoint: {group_endpoint}"
            )
        preferences.multicast_group_endpoint = group_endpoint

    if args.rpc_multicast_ttl is not None:
        if not 0 <= args.rpc_multicast_ttl <= 255:
            raise RuntimeError(
                f"invalid Send2UE RPC multicast TTL: {args.rpc_multicast_ttl}"
            )
        preferences.multicast_ttl = args.rpc_multicast_ttl

    return {
        "before": before,
        "effective": {
            "command_endpoint": str(preferences.command_endpoint),
            "multicast_bind_address": str(
                preferences.command_endpoint
            ).rpartition(":")[0],
            "multicast_group_endpoint": str(preferences.multicast_group_endpoint),
            "multicast_ttl": int(preferences.multicast_ttl),
        },
        "session_only": True,
    }


def write_report(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def file_fingerprint(path):
    candidate = Path(path)
    hasher = hashlib.blake2b(digest_size=16)
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    stat = candidate.stat()
    return {
        "path": str(candidate.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "fingerprint": hasher.hexdigest(),
    }


def export_material_slot_issues(objects):
    """Return empty material slots that Send2UE will reject.

    Limit the check to meshes in the Export collection so harmless source or
    preview objects do not block the handoff.  Send2UE validates every declared
    slot, including an unused empty slot, so the preflight intentionally does
    the same.
    """
    export_collection = bpy.data.collections.get("Export")
    if export_collection is None:
        return []
    export_objects = set(export_collection.all_objects)
    issues = []
    for obj in objects:
        if obj.type != "MESH" or not obj.data:
            continue
        if obj not in export_objects:
            continue
        if len(obj.data.materials) == 0 and len(obj.data.polygons) > 0:
            issues.append({
                "object": obj.name,
                "slot": 0,
                "used_faces": len(obj.data.polygons),
            })
            continue
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


def remove_unused_empty_material_slots(objects):
    """Remove only empty slots that have no polygons, preserving used slots."""
    export_collection = bpy.data.collections.get("Export")
    if export_collection is None:
        return []
    export_objects = set(export_collection.all_objects)
    removed = []
    for obj in objects:
        if obj.type != "MESH" or not obj.data:
            continue
        if obj not in export_objects:
            continue
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


def manifest_asset_files(manifest_assets):
    paths = []
    for manifest_asset in manifest_assets:
        asset_data = manifest_asset.get("asset_data") or {}
        for value in (
            asset_data.get("file_path"),
            asset_data.get("fcurve_file_path"),
            *(asset_data.get("lods") or {}).values(),
        ):
            if value and value not in paths:
                paths.append(value)
    missing = [str(path) for path in paths if not Path(path).is_file()]
    if missing:
        raise RuntimeError("Send2UE exported file missing: " + ", ".join(missing))
    return [file_fingerprint(path) for path in paths]


def command_json_files(manifest_assets):
    """Return absolute JSON dependencies embedded in extension command payloads."""
    pattern = re.compile(r'''(?i)(?:r)?["']([a-z]:[\\/][^"']+\.json)["']''')
    paths = []
    for manifest_asset in manifest_assets:
        command_groups = (
            list(manifest_asset.get("pre_import_commands") or [])
            + list(manifest_asset.get("post_import_commands") or [])
        )
        for commands in command_groups:
            for line in commands:
                for match in pattern.findall(str(line)):
                    path = Path(match)
                    if path.is_file() and path not in paths:
                        paths.append(path)
    return paths


def stable_fingerprint(data):
    encoded = json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def unreal_log_checkpoint():
    log_path = Path(r"C:\UnrealProjects\MyProject2\Saved\Logs\MyProject2.log")
    try:
        return log_path, log_path.stat().st_size
    except OSError:
        return log_path, 0


def target_material_compile_failures(log_path, offset, material_names):
    try:
        with log_path.open("rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    targets = {
        "MI_" + name[2:] if name.startswith("M_") else name
        for name in material_names
        if name
    }
    lines = text.splitlines()
    failures = []
    for index, line in enumerate(lines):
        if "Failed to compile Material Instance" not in line:
            continue
        context = "\n".join(lines[index : index + 16])
        if targets and not any(target in context for target in targets):
            continue
        failures.append(context)
    return failures


def find_export_unit_name():
    """The first prepared Empty under Export names the primary UE asset."""
    coll = bpy.data.collections.get("Export")
    if not coll:
        return None
    for obj in coll.objects:
        if obj.type == "EMPTY" and obj.children:
            return obj.name
    return None


def export_object_wind_facts():
    """Describe only the facts needed to select the DynamicWind handoff."""
    collection = bpy.data.collections.get("Export")
    if collection is None:
        return []
    facts = []
    for obj in collection.all_objects:
        row = {
            "name": obj.name,
            "type": obj.type,
            "cluster_generated": bool(obj.get(CLUSTER_GENERATED_FLAG, False)),
            "asset_role": obj.get(CLUSTER_ASSET_ROLE_KEY),
        }
        if obj.type == "ARMATURE" and obj.data is not None:
            row["bone_names"] = [bone.name for bone in obj.data.bones]
        elif obj.type == "MESH":
            row["vertex_groups"] = [group.name for group in obj.vertex_groups]
        facts.append(row)
    return facts


def unreal_editor_running():
    """Best-effort distinction between an RPC error and an editor crash/exit."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # result.stdout has been observed as None under background Blender
        # even with capture_output=True; a crash here would skip the report.
        return result.returncode == 0 and "UnrealEditor.exe" in (result.stdout or "")
    except Exception:
        return None


def enable_required_addon(addon_utils, module):
    """Enable only the add-ons that the isolated Push pipeline requires."""
    _loaded, enabled = addon_utils.check(module)
    if not enabled:
        bpy.ops.preferences.addon_enable(module=module)


def export_complexity():
    """Return cheap counts for the exact mesh unit that Send2UE will export."""
    export_collection = bpy.data.collections.get("Export")
    objects = list(export_collection.all_objects) if export_collection else []
    meshes = [obj for obj in objects if obj.type == "MESH" and obj.data]
    armatures = set()
    for mesh in meshes:
        armature = mesh.find_armature()
        if armature and armature.data:
            armatures.add(armature)
    return {
        "mesh_count": len(meshes),
        "polygon_count": sum(len(mesh.data.polygons) for mesh in meshes),
        "bone_count": sum(len(armature.data.bones) for armature in armatures),
        "meshes": [
            {"name": mesh.name, "polygons": len(mesh.data.polygons)}
            for mesh in meshes
        ],
    }


def adaptive_rpc_timeout(complexity, polygon_limit, bone_limit, minimum, maximum):
    """Scale the RPC wait continuously from the export's relative complexity."""
    minimum = max(60, float(minimum))
    maximum = max(minimum, float(maximum))
    ratios = []
    if polygon_limit > 0:
        ratios.append(complexity["polygon_count"] / polygon_limit)
    if bone_limit > 0:
        ratios.append(complexity["bone_count"] / bone_limit)
    load_ratio = min(1.0, max(ratios, default=0.0))
    seconds = math.ceil(minimum + (maximum - minimum) * load_ratio)
    return seconds, load_ratio


def run_owned_rpc(run_commands, lines, label):
    """Mark an RPC phase without adding a second timeout implementation."""
    emit_child_progress_marker(
        SEND2UE_RPC_OWNED_START_MARKER, phase=label
    )
    try:
        return run_commands(lines)
    finally:
        emit_child_progress_marker(
            SEND2UE_RPC_OWNED_DONE_MARKER, phase=label
        )


def main():
    args = parse_args()
    report_path = Path(args.report).resolve()
    if not args.manifest:
        args.manifest = str(report_path.with_name(f"{report_path.stem}_manifest.json"))
    if not args.export_root:
        args.export_root = str(report_path.with_name(f"{report_path.stem}_export"))
    if not args.unreal_ingest:
        args.unreal_ingest = str(Path(__file__).resolve().parents[1] / "unreal_ingest.py")
    blend_path = bpy.data.filepath
    report = {
        "blend": blend_path,
        "status": "failed",
        "stage": "startup",
        "unreal_rpc_started": False,
    }
    emit_child_progress_marker(
        SEND2UE_JOB_START_MARKER, transport=args.transport
    )
    try:
        import addon_utils

        # --factory-startup keeps unrelated UI/GPU add-ons out of background
        # Blender, so required handoff registrations must be explicit.
        report["stage"] = "addon_setup"
        enable_required_addon(addon_utils, "speedtree_bone_weight_repair")
        enable_required_addon(addon_utils, "ue_unique_export_names_addon")
        enable_required_addon(addon_utils, "send2ue")
        report["rpc_configuration"] = configure_send2ue_rpc_preferences(args)

        from speedtree_bone_weight_repair.core import (
            consolidate_speedtree_group_materials,
            load_speedtree_texture_readiness_contract,
            normalize_speedtree_material_textures,
        )

        report["stage"] = "texture_validation"
        spm_path = Path(args.spm).resolve()
        blend_dir = Path(blend_path).parent
        cluster_manifest = load_cluster_assembly_manifest(
            blend_dir,
            spm_path,
        )
        report["dependency_orchestrated"] = bool(
            args.dependency_orchestrated
        )
        actionable_cluster_manifest = (
            is_actionable_cluster_assembly_manifest(cluster_manifest)
        )
        if actionable_cluster_manifest and not args.dependency_orchestrated:
            raise RuntimeError(
                "Tree Push with Cluster Assembly must run through the SK Batch "
                "dependency orchestrator; direct send2ue_push_job execution is "
                "blocked so normalized Cluster sources cannot be skipped"
            )
        try:
            speedtree_spm = resolve_cluster_spm_pair(spm_path)["canonical_spm"]
        except ClusterSpmPairPathError:
            speedtree_spm = spm_path
        material_payload = json.loads(
            Path(args.material_contract).read_text(encoding="utf-8")
        )
        material_source = validate_material_handoff_wrapper(
            material_payload,
            speedtree_spm,
        )
        source_fbx = Path(material_source["fbx"])
        report["canonical_spm"] = str(spm_path)
        report["speedtree_spm"] = str(speedtree_spm)
        report["material_source_spm"] = material_source["spm"]
        report["material_source_stmat"] = material_source["stmat"]
        texture_contract = load_speedtree_texture_readiness_contract(
            args.material_contract,
            spm_path=material_source["spm"],
            source_fbx_path=str(source_fbx),
        )
        if not texture_contract.get("strict_speedtree_pipeline_contract"):
            raise RuntimeError(
                "Unreal Push requires a strict SpeedTree pipeline texture contract"
            )
        report["texture_contract"] = {
            "status": texture_contract.get("status"),
            "path": texture_contract.get("contract_path"),
            "strict_speedtree_pipeline_contract": bool(
                texture_contract.get("strict_speedtree_pipeline_contract")
            ),
        }
        export_collection = bpy.data.collections.get("Export")
        export_objects = list(export_collection.all_objects) if export_collection else []
        repair_evidence = None
        if args.repair_evidence:
            try:
                repair_evidence = json.loads(
                    Path(args.repair_evidence).read_text(encoding="utf-8")
                )
                validate_repair_push_evidence_bundle(
                    repair_evidence,
                    expected_queue_spm=spm_path,
                )
                validate_export_object_postcondition(
                    repair_evidence.get("export_objects"),
                    bpy.data,
                )
            except (
                OSError,
                ValueError,
                RepairPushEvidenceError,
            ) as exc:
                raise RuntimeError(
                    stale_execution_freeze_message(exc)
                ) from exc
            report["repair_evidence"] = {
                "status": "verified",
                "schema_version": repair_evidence.get("schema_version"),
                "bundle_sha256": repair_evidence.get("bundle_sha256"),
                "export_coverage": (
                    repair_evidence.get("export_objects") or {}
                ).get("coverage"),
                "push_mutators_skipped": True,
            }
        if repair_evidence is None:
            material_consolidation = consolidate_speedtree_group_materials(
                export_objects,
                texture_contract=texture_contract,
            )
            texture_normalization = normalize_speedtree_material_textures(
                export_objects,
                texture_contract=texture_contract,
            )
        else:
            material_consolidation = {
                "status": "verified_repair_postcondition",
                "changed_count": 0,
            }
            texture_normalization = {
                "status": "ok",
                "missing": [],
                "changed_count": 0,
                "evidence": "exact_repair_export_postcondition",
            }
        report["material_consolidation"] = material_consolidation
        report["texture_normalization"] = texture_normalization
        export_meshes = [
            obj for obj in (export_collection.all_objects if export_collection else [])
            if obj.type == "MESH" and obj.data
        ]
        orphan_owned_export_empties = [
            obj.name
            for obj in (export_collection.objects if export_collection else [])
            if obj.type == "EMPTY"
            and not obj.children
            and bool(obj.get("codex_source_fbx", ""))
        ]
        export_collection_issues = []
        if export_collection is None:
            export_collection_issues.append("missing_export_collection")
        elif not export_meshes:
            export_collection_issues.append("export_collection_has_no_meshes")
        export_collection_issues.extend(
            f"orphan_owned_export_empty:{name}"
            for name in orphan_owned_export_empties
        )
        removed_empty_slots = (
            remove_unused_empty_material_slots(export_objects)
            if repair_evidence is None
            else []
        )
        report["removed_unused_empty_material_slots"] = removed_empty_slots
        empty_material_slots = export_material_slot_issues(export_objects)
        vertex_payload_contracts = (
            [
                pack_speedtree_vertex_payload(
                    obj,
                    mirror_to_nanite_uv=True,
                )
                for obj in export_meshes
            ]
            if repair_evidence is None
            else [
                {
                    "object": row.get("name"),
                    "status": "ok",
                    "changed": False,
                    "evidence": "exact_repair_export_postcondition",
                }
                for row in (
                    repair_evidence.get("export_objects") or {}
                ).get("objects") or ()
                if row.get("type") == "MESH"
            ]
        )
        blocked_vertex_payloads = [
            item for item in vertex_payload_contracts if item.get("status") == "blocked"
        ]
        vertex_color_contracts = [
            inspect_object_vertex_colors(
                obj,
                require_green_signal=args.require_green_signal,
            )
            for obj in export_meshes
        ]
        blocked_vertex_colors = [
            item for item in vertex_color_contracts if item.get("status") == "blocked"
        ]
        report["vertex_payload_contracts"] = vertex_payload_contracts
        report["vertex_color_contracts"] = vertex_color_contracts
        report["export_collection_issues"] = export_collection_issues
        report["handoff_preflight"] = {
            "status": "blocked" if (
                texture_normalization.get("missing")
                or export_collection_issues
                or empty_material_slots
                or blocked_vertex_payloads
                or blocked_vertex_colors
            ) else "ok",
            "export_collection_issues": export_collection_issues,
            "empty_material_slots": empty_material_slots,
            "missing_textures": texture_normalization.get("missing", []),
            "vertex_payload_contracts": vertex_payload_contracts,
            "vertex_color_contracts": vertex_color_contracts,
        }

        # ② Blender Repair owns the source artifact. ③ works on this isolated
        # in-memory scene only, so Push never rewrites a multi-GB .blend and a
        # later manifest/Unreal failure cannot partially mutate the source.
        consolidation_changed = material_consolidation.get("status") == "applied"
        normalization_changed = bool(texture_normalization.get("changed_count"))
        report["in_memory_repair_changes"] = bool(
            consolidation_changed
            or normalization_changed
            or removed_empty_slots
            or any(item.get("changed") for item in vertex_payload_contracts)
        )
        report["blend_resaved"] = False
        missing_textures = texture_normalization.get("missing", [])
        if missing_textures:
            details = []
            for item in missing_textures:
                roles = ", ".join(item.get("missing_roles", [])) or "대응 세트"
                details.append(
                    f"{item.get('material', '?')} -> "
                    f"{item.get('expected_texture_base', 'T_?')} ({roles})"
                )
            raise RuntimeError(
                "PCG ST9 Texture Batch 산출물 누락; Unreal Push 중단: "
                + " | ".join(details)
            )
        if export_collection_issues:
            raise RuntimeError(
                "Send2UE Export collection contract failed: "
                + ", ".join(export_collection_issues)
            )
        if empty_material_slots:
            details = " | ".join(
                f"Mesh '{item['object']}' slot {item['slot']} has no material"
                for item in empty_material_slots
            )
            raise RuntimeError(details)
        if blocked_vertex_colors:
            details = " | ".join(
                f"Mesh '{item.get('object', '?')}': "
                + ", ".join(item.get("issues") or ["unknown vertex color error"])
                for item in blocked_vertex_colors
            )
            raise RuntimeError("Vertex color contract failed: " + details)
        if blocked_vertex_payloads:
            details = " | ".join(
                f"Mesh '{item.get('object', '?')}': "
                + ", ".join(item.get("issues") or ["unknown vertex payload error"])
                for item in blocked_vertex_payloads
            )
            raise RuntimeError("SpeedTree AO/Nanite UV payload failed: " + details)

        # The polygon/bone values are scaling references for the adaptive RPC
        # timeout, not hard limits: over-reference exports proceed at the
        # maximum timeout instead of being skipped as manual_required.
        report["stage"] = "complexity_guard"
        complexity = export_complexity()
        report["complexity"] = complexity
        over_reference = []
        if (
            args.max_push_polygons > 0
            and complexity["polygon_count"] > args.max_push_polygons
        ):
            over_reference.append(
                f"폴리곤 {complexity['polygon_count']:,} > {args.max_push_polygons:,}"
            )
        if args.max_push_bones > 0 and complexity["bone_count"] > args.max_push_bones:
            over_reference.append(
                f"본 {complexity['bone_count']:,} > {args.max_push_bones:,}"
            )
        if over_reference:
            report["complexity_over_reference"] = over_reference
            print(
                "[SK Batch] 복잡도 기준 초과 (최대 RPC 타임아웃으로 계속 진행): "
                + ", ".join(over_reference)
            )

        from send2ue.constants import PathModes
        from send2ue.core import ingest, utilities
        from send2ue.dependencies import unreal as send2ue_unreal_dependency
        from send2ue.dependencies.unreal import run_commands, set_rpc_env

        if not args.send2ue_unreal_py:
            args.send2ue_unreal_py = str(Path(send2ue_unreal_dependency.__file__).resolve())

        if args.transport == "rpc":
            report["stage"] = "unreal_preflight"
            if not utilities.is_unreal_connected():
                raise RuntimeError(
                    "Unreal editor is not running / RPC not reachable. Open the project first."
                )
            report["unreal_rpc_started"] = True

            rpc_timeout, rpc_load_ratio = adaptive_rpc_timeout(
                complexity,
                args.max_push_polygons,
                args.max_push_bones,
                args.rpc_timeout_min,
                args.rpc_timeout_max,
            )
            set_rpc_env("RPC_TIME_OUT", rpc_timeout)
            report["rpc_timeout_seconds"] = rpc_timeout
            report["rpc_timeout"] = {
                "seconds": rpc_timeout,
                "minimum": max(60, float(args.rpc_timeout_min)),
                "maximum": max(
                    max(60, float(args.rpc_timeout_min)),
                    float(args.rpc_timeout_max),
                ),
                "load_ratio": round(rpc_load_ratio, 6),
                "polygon_ratio": round(
                    complexity["polygon_count"] / args.max_push_polygons, 6
                ) if args.max_push_polygons > 0 else None,
                "bone_ratio": round(
                    complexity["bone_count"] / args.max_push_bones, 6
                ) if args.max_push_bones > 0 else None,
            }
            print(
                f"[SK Batch] RPC timeout: {rpc_timeout}s "
                f"(load {rpc_load_ratio:.1%}, range "
                f"{args.rpc_timeout_min:g}-{args.rpc_timeout_max:g}s)"
            )
        else:
            rpc_timeout = 0

        scene_props = bpy.context.scene.send2ue
        folder_before_sync = scene_props.unreal_mesh_folder_path
        # A saved template or an earlier output layout can leave this value on
        # the FBX staging folder.  Re-derive it from the actual .blend location
        # immediately before Send2UE builds the manifest.
        utilities.sync_unreal_mesh_folder_path()
        if hasattr(scene_props, "skip_animation_export"):
            scene_props.skip_animation_export = True

        unit_name = find_export_unit_name() or spm_path.stem
        declared_unit_name = unit_name
        folder = scene_props.unreal_mesh_folder_path
        report["unit_name"] = unit_name
        report["declared_export_unit_name"] = declared_unit_name
        report["unreal_folder"] = folder
        report["unreal_folder_before_sync"] = folder_before_sync
        report["unreal_folder_synced"] = folder != folder_before_sync
        report["transport"] = args.transport

        export_root = Path(args.export_root).resolve()
        export_root.mkdir(parents=True, exist_ok=True)
        scene_props.path_mode = PathModes.SEND_TO_DISK.value
        scene_props.disk_mesh_folder_path = str(export_root)
        scene_props.disk_animation_folder_path = str(export_root / "animations")
        scene_props.disk_groom_folder_path = str(export_root / "groom")

        mesh_path = folder.rstrip("/") + "/" + unit_name
        # BWR writes the final-skeleton wind contract from the selected SPM
        # identity. Never derive it from a mutable Blender object name.
        wind_source_stem = spm_path.stem
        wind_json = (
            blend_dir
            / "JSON"
            / f"{wind_source_stem}_dynamic_wind_import_from_megaplant_groups.json"
        )
        report["wind_source_stem"] = wind_source_stem

        report["stage"] = "send2ue_export"
        emit_child_progress_marker(
            SEND2UE_DISK_EXPORT_START_MARKER, unit=unit_name
        )
        result = bpy.ops.wm.send2ue("EXEC_DEFAULT")
        if "FINISHED" not in result:
            raise RuntimeError(f"send2ue returned {result}")
        report["send2ue"] = "FINISHED"
        emit_child_progress_marker(
            SEND2UE_DISK_EXPORT_DONE_MARKER, unit=unit_name
        )

        report["stage"] = "manifest"
        manifest_assets = ingest.build_manifest_items(scene_props)
        if not manifest_assets:
            raise RuntimeError("Send2UE produced no manifest assets")
        report["manifest_handoff_normalization"] = (
            normalize_manifest_handoff_sidecars(
                manifest_assets,
                export_root,
                sidecar_descriptor_builder=(
                    shared_contract_api().build_sidecar_descriptor
                ),
            )
        )
        mesh_path = primary_mesh_asset_path(
            manifest_assets,
            preferred_path=mesh_path,
        )
        unit_name = mesh_path.rsplit("/", 1)[-1]
        report["unit_name"] = unit_name
        report["mesh_path"] = mesh_path

        cluster_assembly = None
        material_asset_scope = None
        if actionable_cluster_manifest:
            material_asset_scope = scope_material_pipeline_for_destination(
                manifest_assets,
                folder,
                export_root,
            )
            report["material_asset_scope"] = material_asset_scope
            full_templates = [
                asset
                for asset in manifest_assets
                if (asset.get("asset_data") or {}).get("_asset_type")
                == "SkeletalMesh"
                and (asset.get("asset_data") or {}).get("asset_path") == mesh_path
            ]
            if len(full_templates) != 1:
                raise RuntimeError(
                    "Cluster Assembly requires exactly one Full SK Send2UE manifest asset"
                )
            cluster_plan = build_unreal_ingest_plan(
                cluster_manifest,
                full_templates[0],
                mesh_path,
                folder,
            )
            cluster_assembly = {
                "manifest": cluster_manifest,
                "ingest_plan": cluster_plan,
            }

        cluster_assembly_status = (
            cluster_assembly["ingest_plan"].get("status")
            if cluster_assembly is not None
            else None
        )
        wind_policy = resolve_dynamic_wind_policy(
            export_object_wind_facts(),
            explicit_skip=args.skip_wind,
            cluster_assembly_status=cluster_assembly_status,
        )
        wind_json_enabled = bool(wind_policy["requires_json"])
        report["wind_policy"] = wind_policy

        exported_files = manifest_asset_files(manifest_assets)
        if cluster_assembly is not None:
            for generated in (cluster_assembly["ingest_plan"].get("assets") or []):
                source = (generated.get("asset_data") or {}).get("file_path")
                if source:
                    exported_files.append(file_fingerprint(source))
        json_dir = blend_dir / "JSON"
        handoff_paths = []
        if json_dir.is_dir():
            handoff_stems = {
                unit_name.casefold(),
                wind_source_stem.casefold(),
                speedtree_spm.stem.casefold(),
            }
            handoff_paths = [
                path
                for path in sorted(json_dir.glob("*.json"))
                if any(
                    stem in path.stem.casefold()
                    for stem in handoff_stems
                )
                and (
                    wind_json_enabled
                    or path.resolve() != wind_json.resolve()
                )
            ]
        for path in command_json_files(manifest_assets):
            if path not in handoff_paths:
                handoff_paths.append(path)
        handoff_files = [file_fingerprint(path) for path in handoff_paths]
        wind_file = (
            None
            if not wind_json_enabled or not wind_json.is_file()
            else file_fingerprint(wind_json)
        )
        if wind_json_enabled and wind_file is None:
            raise RuntimeError(
                "required final-skeleton dynamic wind JSON missing: "
                + str(wind_json)
            )
        if cluster_assembly is not None and cluster_assembly["ingest_plan"].get("status") == "ready":
            expected_wind = (
                cluster_assembly["manifest"].get("wind_contract") or {}
            ).get("wind_json")
            validate_file_fingerprint(expected_wind, "BWR final Skeleton wind JSON")
            if wind_file is None:
                raise RuntimeError(
                    "Cluster Assembly requires the Full SK final Skeleton wind JSON"
                )
            canonical_full_wind = cluster_file_fingerprint(wind_json)
            if (
                int(canonical_full_wind.get("size") or -1)
                != int((expected_wind or {}).get("size") or -2)
                or str(canonical_full_wind.get("sha256") or "").casefold()
                != str((expected_wind or {}).get("sha256") or "").casefold()
            ):
                raise RuntimeError(
                    "Full SK and Assembly wind JSON fingerprints do not match"
                )
        code_files = [
            file_fingerprint(args.unreal_ingest),
            file_fingerprint(args.send2ue_unreal_py),
        ]
        wind_policy_module = (
            Path(__file__).resolve().parents[1]
            / "dynamic_wind_handoff_policy.py"
        )
        if wind_policy_module.is_file():
            code_files.append(file_fingerprint(wind_policy_module))
        cluster_builder = Path(__file__).resolve().parents[1] / "cluster_assembly_builder.py"
        if cluster_builder.is_file():
            code_files.append(file_fingerprint(cluster_builder))
        material_pipeline = (
            Path(args.send2ue_unreal_py).resolve().parents[1]
            / "resources"
            / "pipeline"
            / "ue_material_setup.py"
        )
        if material_pipeline.is_file():
            code_files.append(file_fingerprint(material_pipeline))

        contract = {
            "schema_version": 1,
            "source_fingerprint": args.source_fingerprint,
            "blend": str(Path(blend_path).resolve()),
            "unit_name": unit_name,
            "unreal_folder": folder,
            "mesh_path": mesh_path,
            "assets": manifest_assets,
            "exported_files": exported_files,
            "handoff_files": handoff_files,
            "wind_file": wind_file,
            "wind_policy": wind_policy,
            "code_files": code_files,
            "cluster_assembly": cluster_assembly,
            "dependency_orchestrated": bool(args.dependency_orchestrated),
            "material_asset_scope": material_asset_scope,
        }
        fingerprint = stable_fingerprint(contract)
        queue_id = args.queue_id or str(Path(blend_path).resolve())
        item_import_report = args.item_import_report or str(
            Path(args.report).with_name(
                f"{Path(args.report).stem}_unreal.json"
            )
        )
        item = {
            **contract,
            "queue_id": queue_id,
            "fingerprint": fingerprint,
            "send2ue_unreal_py": str(Path(args.send2ue_unreal_py).resolve()),
            "wind_json": str(wind_json) if wind_json_enabled else None,
            "checkout_asset_paths": manifest_checkout_asset_paths(
                manifest_assets
            ) + (
                [
                    cluster_assembly["ingest_plan"]["asset_contract"][key]
                    for key in ("base_skeletal_mesh", "assembly")
                ]
                + list(
                    cluster_assembly["ingest_plan"]["asset_contract"]["parts"].values()
                )
                + [
                    path + "_Skeleton"
                    for path in cluster_assembly["ingest_plan"]["asset_contract"][
                        "parts"
                    ].values()
                ]
                if cluster_assembly
                and cluster_assembly["ingest_plan"].get("status") == "ready"
                else []
            ),
            "report_path": str(Path(item_import_report).resolve()),
            "export_report_path": str(Path(args.report).resolve()),
        }
        manifest_path = Path(args.manifest).resolve()
        checkpoint_path = Path(
            args.checkpoint
            or manifest_path.with_name(f"{manifest_path.stem}_checkpoint.json")
        ).resolve()
        batch_report_path = Path(
            args.batch_report
            or manifest_path.with_name(f"{manifest_path.stem}_report.json")
        ).resolve()
        manifest = {
            "schema_version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "checkpoint_path": str(checkpoint_path),
            "report_path": str(batch_report_path),
            "max_item_crash_retries": 2,
            "items": [item],
        }
        write_report(manifest_path, manifest)
        report["manifest"] = str(manifest_path)
        report["manifest_fingerprint"] = fingerprint
        report["exported_files"] = exported_files
        report["handoff_files"] = handoff_files
        report["checkpoint"] = str(checkpoint_path)
        report["batch_report"] = str(batch_report_path)
        report["item_import_report"] = str(Path(item_import_report).resolve())

        if args.transport == "headless_export":
            report["stage"] = "exported_pending_unreal"
            report["status"] = "exported_pending_unreal"
            report["failure_kind"] = None
        else:
            if batch_report_path.exists():
                batch_report_path.unlink()
            runner_path = str(Path(args.unreal_ingest).resolve())
            runner_load_lines = [
                "import importlib.util, sys",
                # Unreal keeps embedded-Python modules alive across RPC jobs.
                # Purge the Assembly contract module before loading the runner
                # so a newly normalized public path/name cannot be validated by
                # stale code from an earlier Push in the same Editor session.
                "sys.modules.pop('cluster_assembly_builder', None)",
                f"_runner_path = {runner_path!r}",
                "_spec = importlib.util.spec_from_file_location('sk_batch_unreal_ingest_rpc', _runner_path)",
                "if _spec is None or _spec.loader is None:",
                "\traise RuntimeError('Could not load SK Batch Unreal ingest: ' + _runner_path)",
                "_runner = importlib.util.module_from_spec(_spec)",
                "sys.modules[_spec.name] = _runner",
                "_spec.loader.exec_module(_runner)",
            ]
            rpc_lines = runner_load_lines + [
                (
                    f"_runner.run_manifest({str(manifest_path)!r}, "
                    f"checkpoint_path={str(checkpoint_path)!r}, "
                    f"report_path={str(batch_report_path)!r})"
                ),
            ]
            report["stage"] = "rpc_ingest"
            run_owned_rpc(run_commands, rpc_lines, "manifest_ingest")
            if not batch_report_path.is_file():
                raise RuntimeError(
                    "SK Batch RPC manifest ingest returned without a result file"
                )
            batch_result = json.loads(
                batch_report_path.read_text(encoding="utf-8")
            )
            item_result = (batch_result.get("items") or {}).get(queue_id, {})
            if item_result.get("status") == "runtime_pending":
                report["stage"] = "rpc_runtime_frame_validation"
                for runtime_attempt in range(1, 31):
                    time.sleep(0.1)
                    run_owned_rpc(
                        run_commands,
                        runner_load_lines
                        + [
                            (
                                f"_runner.finish_runtime_probe({str(checkpoint_path)!r}, "
                                f"{str(batch_report_path)!r}, {queue_id!r})"
                            )
                        ],
                        f"runtime_probe_{runtime_attempt}",
                    )
                    batch_result = json.loads(
                        batch_report_path.read_text(encoding="utf-8")
                    )
                    item_result = (batch_result.get("items") or {}).get(
                        queue_id, {}
                    )
                    report["runtime_finish_attempts"] = runtime_attempt
                    if item_result.get("status") != "runtime_pending":
                        break
                if item_result.get("status") == "runtime_pending":
                    timeout_reason = (
                        "runtime probe did not complete within 30 cross-frame RPC attempts"
                    )
                    run_owned_rpc(
                        run_commands,
                        runner_load_lines
                        + [
                            (
                                f"_runner.cancel_runtime_probe({str(checkpoint_path)!r}, "
                                f"{str(batch_report_path)!r}, {queue_id!r}, "
                                f"{timeout_reason!r})"
                            )
                        ],
                        "runtime_probe_cancel",
                    )
                    batch_result = json.loads(
                        batch_report_path.read_text(encoding="utf-8")
                    )
                    item_result = (batch_result.get("items") or {}).get(
                        queue_id, {}
                    )
            report["unreal_result"] = item_result
            report["wind"] = item_result.get("wind")
            report["materials"] = item_result.get("materials")
            report["checkout"] = item_result.get("checkout")
            if item_result.get("status") != "imported_ok":
                report["status"] = item_result.get("status", "failed")
                report["failure_kind"] = item_result.get("status", "data_error")
                raise RuntimeError(
                    item_result.get("message") or "Unreal manifest ingest failed"
                )
            report["stage"] = "completed"
            report["status"] = "ok"
            report["failure_kind"] = None
    except Exception as exc:
        # Record the error before any classification helper runs: a crash in
        # this handler must never leave the report unwritten ("job report was
        # not created" hides the real failure from the GUI).
        emit_child_progress_marker(
            SEND2UE_JOB_FAILED_MARKER, stage=report.get("stage", "unknown")
        )
        error_text = str(exc)
        report["error"] = error_text
        report["traceback"] = traceback.format_exc()
        try:
            classify_failure(report, error_text)
        except Exception:
            report.setdefault("failure_kind", "data_error")
    write_report(args.report, report)
    emit_child_progress_marker(
        SEND2UE_JOB_DONE_MARKER, status=report.get("status", "failed")
    )
    if report["status"] not in {"ok", "exported_pending_unreal"}:
        sys.exit(1)


def classify_failure(report, error_text):
    editor_running = unreal_editor_running()
    report["unreal_editor_running_after_failure"] = editor_running
    lowered = error_text.lower()
    if report.get("status") == "manual_required":
        failure_kind = "manual_required"
    elif (
        report.get("stage") != "unreal_preflight"
        and report.get("unreal_rpc_started")
        and editor_running is False
    ):
        failure_kind = "unreal_crash"
    elif any(
        token in lowered
        for token in (
            'the call "import_asset" timed out',
            "rpc timeout",
            "rpc_time_out",
            "no result file (timed out",
        )
    ):
        failure_kind = "rpc_timeout"
    elif any(
        token in lowered
        for token in (
            "could not find an open unreal editor instance",
            "rpc not reachable",
            "unreal editor is not running",
            "connectionreseterror",
            "winerror 10054",
        )
    ):
        failure_kind = "unreal_unavailable"
    else:
        failure_kind = "data_error"
    report["failure_kind"] = failure_kind
    if failure_kind == "unreal_crash":
        report["error"] = f"Unreal Editor crashed or exited during push: {error_text}"


main()
