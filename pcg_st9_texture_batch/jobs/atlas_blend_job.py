"""② 헤드리스 Blender 잡: 아틀라스 리프 제너레이터로 잎 메시 blend 생성.

실행:
  blender -b --python atlas_blend_job.py -- --albedo A --alpha B \
      --material-name M_x_atlas_01 --blend-out out.blend --report r.json
      [--quality SPEEDTREE_LOW] [--plate-mode SINGLE]
      [--spm SK_x.spm ...] [--build-spm]

pair(front island) 목록은 비워서 넘긴다 → 애드온이 감지한 모든 알파 아일랜드를
잎으로 사용한다 (helper_pipeline: pairs가 비면 전체 컴포넌트 사용).
--build-spm 을 줄 때만 SK SPM에 잎 메시를 반영한다(Build/Update Target SPMs).
"""
import argparse
import json
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

import addon_utils
import bpy


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--albedo", required=True)
    parser.add_argument("--alpha", required=True)
    parser.add_argument("--material-name", required=True)
    parser.add_argument("--blend-out", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--quality", default="SPEEDTREE_LOW")
    parser.add_argument("--plate-mode", default="SINGLE")
    parser.add_argument("--spm", action="append", default=[])
    parser.add_argument("--build-spm", action="store_true")
    parser.add_argument("--work-dir", default="")
    return parser.parse_args(argv)


def backup_spms(paths):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backups = []
    for value in paths:
        source = Path(value)
        if not source.exists():
            raise RuntimeError(f"SPM 대상 없음: {source}")
        backup_dir = source.parent / "_spm_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{source.stem}.pcgtex_backup_before_atlas_build_{stamp}.spm"
        shutil.copy2(source, backup)
        backups.append((source, backup))
    return backups


def restore_spms(backups):
    restored = []
    for target, backup in backups:
        shutil.copy2(backup, target)
        restored.append(str(target))
    return restored


def main():
    args = parse_args()
    report = {"status": "error"}
    spm_backups = []
    try:
        enabled = addon_utils.enable("atlas_leaf_mesh_builder", default_set=False, persistent=False)
        if enabled is None:
            raise RuntimeError("애드온 atlas_leaf_mesh_builder 활성화 실패 (Blender addons 설치 확인)")

        props = bpy.context.scene.atlas_leaf_builder
        props.albedo_path = args.albedo
        props.alpha_path = args.alpha
        work_dir = args.work_dir or str(Path(args.blend_out).parent / "_atlas_job_work")
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        props.output_dir = work_dir
        props.quality = args.quality
        props.surface_mode = args.plate_mode
        props.collection_name = args.material_name
        props.speedtree_material_name = args.material_name
        props.clear_existing = True
        # pair 비우기 → 모든 알파 아일랜드 사용
        props.pair_items.clear()
        props.pair_json = "[]"

        result = bpy.ops.atlas_leaf.generate()
        if "FINISHED" not in result:
            raise RuntimeError(f"generate 실패: {result}")
        collection = bpy.data.collections.get(args.material_name)
        mesh_count = len([o for o in collection.objects if o.type == "MESH"]) if collection else 0
        if mesh_count == 0:
            raise RuntimeError("잎 메시가 생성되지 않음 (알파 아일랜드 감지 실패?)")

        props.speedtree_spm_items.clear()
        for spm in args.spm:
            item = props.speedtree_spm_items.add()
            item.path = spm

        blend_out = Path(args.blend_out)
        blend_out.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_out))

        spm_summary = None
        if args.build_spm and args.spm:
            spm_backups = backup_spms(args.spm)
            spm_result = bpy.ops.atlas_leaf.build_speedtree_spm()
            if "FINISHED" not in spm_result:
                raise RuntimeError(f"SPM 반영 실패: {spm_result}")
            spm_summary = props.last_report
            # scope UUID가 blend에 저장되도록 반영 후 다시 저장
            bpy.ops.wm.save_mainfile()

        report = {
            "status": "ok",
            "blend": str(blend_out),
            "meshes": mesh_count,
            "quality": args.quality,
            "plate_mode": args.plate_mode,
            "spm_targets": args.spm,
            "spm_built": bool(args.build_spm and args.spm),
            "spm_report": spm_summary,
            "spm_backups": [str(backup) for _target, backup in spm_backups],
        }
    except Exception as exc:
        restored = []
        if spm_backups:
            try:
                restored = restore_spms(spm_backups)
            except Exception as restore_exc:
                restored.append(f"복원 실패: {restore_exc}")
        report = {
            "status": "error", "error": str(exc), "traceback": traceback.format_exc(),
            "spm_backups": [str(backup) for _target, backup in spm_backups],
            "spm_restored": restored,
        }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if report["status"] != "ok":
        sys.exit(1)


main()
