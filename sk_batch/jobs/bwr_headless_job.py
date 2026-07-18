"""Headless Blender job: SPM -> SpeedTree CLI export -> BWR import/repair -> .blend.

Run:
  blender.exe -b --python bwr_headless_job.py -- --spm X.spm --blend X.blend
      --wind TREE|BUSH|GRASS|NONE --report result.json

Runs with --factory-startup and enables only the junction-installed
speedtree_bone_weight_repair add-on for this process.  This avoids loading every
interactive user add-on in a background batch. Re-running on an existing .blend
is a clean idempotent update (the operator wipes its previous build).
"""
import argparse
import json
import os
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
from spm_leaf_handoff_contract import (
    inspect_speedtree_material_export,
    inspect_spm_leaf_contract,
    leaf_contract_user_message,
)
from speedtree_pipeline_contract import validate_preflight_report


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
    parser.add_argument("--blend", required=True)
    parser.add_argument("--wind", default="GRASS", choices=["TREE", "BUSH", "GRASS", "NONE"])
    parser.add_argument("--material-contract", default="")
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def write_report(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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


def main():
    args = parse_args()
    report = {"spm": args.spm, "blend": args.blend, "wind": args.wind, "status": "failed"}
    try:
        material_preflight = None
        if args.material_contract:
            # Validate the exact SPM/STMAT hashes before Blender or the add-on
            # can mutate a scene.  The add-on still receives the same report
            # path for its existing texture-binding loader.
            material_preflight = validate_preflight_report(
                args.material_contract,
                args.spm,
                require_ok=True,
            )
            report["speedtree_pipeline_contract"] = material_preflight[
                "speedtree_pipeline_contract"
            ]
            report["speedtree_pipeline_contract_required"] = True
        import addon_utils

        _default_enabled, loaded = addon_utils.check("speedtree_bone_weight_repair")
        if not loaded:
            addon_utils.enable(
                "speedtree_bone_weight_repair",
                default_set=False,
                persistent=False,
            )
        _default_enabled, loaded = addon_utils.check("speedtree_bone_weight_repair")
        if not loaded:
            raise RuntimeError("speedtree_bone_weight_repair add-on enable failed")

        # Reject authored-but-disabled Branch skeletons before creating an
        # empty .blend. BranchMesh-only assets remain valid and use the rigid
        # one-bone fallback inside the add-on.
        from speedtree_bone_weight_repair.core import require_spm_sk_ready

        require_spm_sk_ready(os.path.abspath(args.spm))

        blend_path = os.path.abspath(args.blend)
        blend_exists = os.path.exists(blend_path)
        if blend_exists:
            bpy.ops.wm.open_mainfile(filepath=blend_path)
        else:
            bpy.ops.wm.read_homefile(use_empty=True)

        settings = bpy.context.scene.speedtree_bwr_settings
        settings.spm_path = os.path.abspath(args.spm)
        settings.texture_contract_path = (
            os.path.abspath(args.material_contract)
            if args.material_contract
            else ""
        )
        if args.wind == "NONE":
            # Dead vegetation: keep the JSON contract but zero all sway.
            settings.wind_preset = "CUSTOM"
            settings.dynamic_wind_flexibility = 0.0
            settings.dynamic_wind_gust_attenuation = 0.0
            settings.dynamic_wind_ground_cover = False
        else:
            settings.wind_preset = args.wind
        settings.write_unreal_json = True
        settings.write_dynamic_wind_json = True

        # default_paths anchors out_dir/JSON to bpy.data.filepath. open_mainfile
        # already set it for existing blends, so only a fresh file needs the
        # anchor save — re-saving a multi-GB blend right before the export
        # rebuilds it was pure disk churn (2x full writes per repair).
        if not blend_exists:
            Path(blend_path).parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=blend_path)

        result = bpy.ops.speedtree_bwr.export_from_speedtree()
        if "FINISHED" not in result:
            raise RuntimeError(f"export_from_speedtree returned {result}")

        stem = Path(args.spm).stem
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
        leaf_reference_contract = inspect_spm_leaf_contract(args.spm)
        material_export_contract = inspect_speedtree_material_export(
            args.spm, leaf_reference_contract
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
            if material_preflight is not None:
                pipeline_data["speedtree_pipeline_contract"] = (
                    material_preflight["speedtree_pipeline_contract"]
                )
                pipeline_data["speedtree_pipeline_contract_required"] = True
            texture_normalization = pipeline_data.get("texture_normalization") or {}
            report["texture_normalization"] = texture_normalization
            report["texture_readiness_contract"] = {
                "status": texture_normalization.get("texture_contract_status", ""),
                "path": texture_normalization.get("texture_contract_path", ""),
            }
            for missing in texture_normalization.get("missing", []):
                roles = ", ".join(missing.get("missing_roles", [])) or "대응 세트"
                report.setdefault("warnings", []).append(
                    f"{missing.get('material', '?')}: "
                    f"{missing.get('expected_texture_base', 'T_?')} ({roles}) 누락"
                )
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
            vertex_payload_contract.get("status") == "ok"
            and vertex_color_contract.get("status") == "ok"
            and not empty_material_slots
            and not leaf_reference_blocked
            and not material_export_blocked
        ):
            bpy.ops.wm.save_as_mainfile(filepath=blend_path)
            report["blend_resaved"] = True

        missing_outputs = [
            report[key]
            for key in ("megaplant_json", "dynamic_wind_json", "pipeline_report")
            if not os.path.exists(report[key])
        ]
        for path in missing_outputs:
            report.setdefault("warnings", []).append(f"expected output missing: {path}")

        preflight = {
            "status": "blocked" if (
                texture_normalization.get("missing")
                or empty_material_slots
                or missing_outputs
                or vertex_color_contract.get("status") == "blocked"
                or vertex_payload_contract.get("status") == "blocked"
                or leaf_reference_blocked
                or material_export_blocked
            ) else "ok",
            "empty_material_slots": empty_material_slots,
            "missing_textures": texture_normalization.get("missing", []),
            "missing_outputs": missing_outputs,
            "vertex_color_contract": vertex_color_contract,
            "vertex_payload_contract": vertex_payload_contract,
            "leaf_reference_contract": leaf_reference_contract,
            "material_export": material_export_contract,
            "missing_materials": material_export_contract.get(
                "missing_materials", []
            ),
        }
        report["handoff_preflight"] = preflight
        if pipeline_data is not None:
            pipeline_data["handoff_preflight"] = preflight
            write_report(pipeline_path, pipeline_data)
        if preflight["status"] != "ok":
            reasons = []
            if texture_normalization.get("missing"):
                reasons.append(f"텍스처 세트 {len(texture_normalization['missing'])}개 미준비")
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
        report["status"] = "failed"
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
    write_report(args.report, report)
    if report["status"] != "ok":
        sys.exit(1)


main()
