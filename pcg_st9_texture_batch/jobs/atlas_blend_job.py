"""② 헤드리스 Blender 잡: 아틀라스 리프 제너레이터로 잎 메시 blend 생성.

실행:
  blender -b --python atlas_blend_job.py -- --albedo A --alpha B \
      --material-name M_x_atlas_01 --blend-out out.blend --report r.json
      [--quality SPEEDTREE_LOW] [--plate-mode SINGLE]
      [--spm SK_x.spm ...] [--build-spm]
      [--target-map-json targets.json] [--reuse-existing-blend]

pair(front island) 목록은 비워서 넘긴다 → 애드온이 감지한 모든 알파 아일랜드를
잎으로 사용한다 (helper_pipeline: pairs가 비면 전체 컴포넌트 사용).
--build-spm 을 줄 때만 SK SPM에 잎 메시를 반영한다(Build/Update Target SPMs).
target-map JSON이 있으면 각 최종 SK의 원본 머티리얼을 정확히 찾아
Leaf Mesh/Frond Generator의 Material/Mesh 슬롯까지 검증하며 연결한다.
"""
import argparse
import inspect
import json
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

import addon_utils
import bpy

TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = TOOL_DIR.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

from mutation_plan_authority import (
    require_child_payload,
    validate_child_authority,
)


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
    parser.add_argument("--target-map-json", default="")
    parser.add_argument("--reuse-existing-blend", action="store_true")
    parser.add_argument("--registry-managed-externally", action="store_true")
    parser.add_argument("--producer-refresh-proof-json", default="")
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--authority-json", required=True)
    parser.add_argument("--authority-sha256", required=True)
    return parser.parse_args(argv)


def load_target_map(path):
    if not path:
        return []
    source = Path(path)
    if not source.is_file():
        raise RuntimeError(f"Generator 연결 대상 JSON 없음: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("targets"), list):
        raise RuntimeError("Generator 연결 대상 JSON 형식이 올바르지 않음 (version 1 필요)")
    targets = []
    seen = set()
    for row in payload["targets"]:
        if not isinstance(row, dict) or not row.get("spm"):
            raise RuntimeError("Generator 연결 대상 JSON에 SPM 경로가 없는 항목이 있음")
        spm = str(Path(row["spm"]))
        key = spm.lower()
        if key in seen:
            raise RuntimeError(f"Generator 연결 대상 JSON에 SPM 중복: {spm}")
        names = [str(value).strip() for value in row.get("source_material_names", [])
                 if str(value).strip()]
        if not names:
            raise RuntimeError(f"원본 머티리얼 식별 정보 없음: {Path(spm).name}")
        seen.add(key)
        targets.append({
            "spm": spm,
            "source_material_names": names,
            "source_material_ids": list(row.get("source_material_ids") or []),
            "generator_bindings": list(row.get("generator_bindings") or []),
        })
    return targets


def backup_spms(paths):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backups = []
    seen = set()
    for value in paths:
        source = Path(value)
        key = str(source).lower()
        if key in seen:
            continue
        seen.add(key)
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


def backup_existing_blend(path):
    source = Path(path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = source.parent / "_blend_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{source.stem}.before_pcg_generator_connect_{stamp}.blend"
    shutil.copy2(source, backup)
    return backup


def collection_mesh_objects(collection):
    objects = getattr(collection, "all_objects", None)
    if objects is None:
        objects = collection.objects
    return [obj for obj in objects if obj.type == "MESH"]


def mesh_collection_for_reuse(props, material_name):
    candidates = []
    for name in (material_name, props.collection_name):
        collection = bpy.data.collections.get(name)
        if collection and collection_mesh_objects(collection):
            if collection not in candidates:
                candidates.append(collection)
    if not candidates:
        mesh_collections = [
            collection for collection in bpy.data.collections
            if collection_mesh_objects(collection)
        ]
        child_collections = {
            child for parent in bpy.data.collections for child in parent.children
        }
        candidates = [
            collection for collection in mesh_collections
            if collection not in child_collections
        ] or mesh_collections
    if len(candidates) != 1:
        names = ", ".join(collection.name for collection in candidates) or "없음"
        raise RuntimeError(
            "기존 blend에서 사용할 잎 메시 Collection을 하나로 결정할 수 없음: "
            f"{names}")
    return candidates[0]


def validate_target_paths(cli_paths, mapped_targets):
    if not mapped_targets:
        return
    cli = {str(Path(value)).lower() for value in cli_paths}
    mapped = {str(Path(row["spm"])).lower() for row in mapped_targets}
    if cli and cli != mapped:
        raise RuntimeError("--spm 목록과 --target-map-json의 최종 SK 목록이 다름")


def apply_mapped_targets(props, targets, material_name):
    try:
        from atlas_leaf_mesh_builder.speedtree import (
            export_or_update_speedtree_spm_path,
        )
    except Exception as exc:
        raise RuntimeError(f"아틀라스 애드온 SpeedTree 연결 모듈 로드 실패: {exc}") from exc

    parameters = inspect.signature(export_or_update_speedtree_spm_path).parameters
    required_parameters = {
        "atlas_asset_name", "source_material_names", "source_material_ids",
        "allow_create",
    }
    if not required_parameters.issubset(parameters):
        raise RuntimeError(
            "설치된 atlas_leaf_mesh_builder가 Generator 연결 API를 지원하지 않음")

    results = []
    for target in targets:
        exported = export_or_update_speedtree_spm_path(
            props,
            target["spm"],
            atlas_asset_name=material_name,
            source_material_names=target["source_material_names"],
            source_material_ids=target["source_material_ids"],
            allow_create=False,
        )
        manifest_path = Path(exported[1])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        connection = manifest.get("generator_connection") or {}
        if connection.get("requested") is not True or connection.get("complete") is not True:
            raise RuntimeError(
                f"Generator 연결 검증 실패: {Path(target['spm']).name}")
        results.append({
            "spm": target["spm"],
            "source_material_names": target["source_material_names"],
            "expected_source_material_ids": target["source_material_ids"],
            "expected_generator_bindings": target["generator_bindings"],
            "manifest": str(manifest_path),
            "generator_connection": connection,
        })
    return results


def apply_exact_assets_only_target(
    props,
    target_spm,
    material_name,
    producer_refresh_proof,
):
    """Atomically update exactly one SPM, independent of the props registry.

    The add-on's ordinary one-path helper rolls back only the SPM itself.  An
    Atlas export also writes meshes plus global, target, and scope receipts,
    so the exact repair deliberately enters the add-on's staged filesystem
    transaction while still passing just the one canonical target.
    """

    from atlas_leaf_mesh_builder.speedtree import (
        _export_or_update_speedtree_spm_path_impl,
        _validate_staged_speedtree_targets,
        target_manifest_path,
    )
    from atlas_leaf_mesh_builder.speedtree_transaction import (
        cleanup_pending_transaction_roots,
        execute_atomic_target_update,
    )
    from atlas_producer_rebind import (
        validate_atlas_producer_refresh_manifest,
    )

    exact_target = Path(target_spm).expanduser().absolute()

    def build_staged_target(staged_target, production_target):
        return _export_or_update_speedtree_spm_path_impl(
            props,
            staged_target,
            atlas_asset_name=material_name,
            source_material_names=None,
            source_material_ids=None,
            allow_create=False,
            production_target_spm=production_target,
        )

    def validate_staged_target(staged_targets, states):
        references = _validate_staged_speedtree_targets(
            staged_targets,
            states,
        )
        if len(staged_targets) != 1:
            raise RuntimeError(
                "producer refresh transaction escaped its one-target boundary"
            )
        staged_manifest = target_manifest_path(staged_targets[0])
        payload = json.loads(staged_manifest.read_text(encoding="utf-8"))
        canonical_manifest = target_manifest_path(exact_target)
        validate_atlas_producer_refresh_manifest(
            producer_refresh_proof,
            payload,
            manifest_path=canonical_manifest,
        )
        return references

    try:
        results = execute_atomic_target_update(
            [exact_target],
            build_staged_target,
            validate_staged_target,
            allow_create=False,
        )
    finally:
        cleanup_pending_transaction_roots()
    if not isinstance(results, list) or len(results) != 1:
        raise RuntimeError(
            "producer refresh transaction returned a non-exact result set"
        )
    return results[0]


def main():
    args = parse_args()
    report = {"status": "error"}
    spm_backups = []
    blend_backup = None
    try:
        authority = validate_child_authority(
            args.authority_json,
            args.authority_sha256,
        )
        require_child_payload(authority, {
            "albedo": str(args.albedo),
            "alpha": str(args.alpha),
            "material_name": str(args.material_name),
            "blend_out": str(args.blend_out),
            "spms": [str(path) for path in args.spm],
            "target_map_json": str(args.target_map_json),
            "build_spm": bool(args.build_spm),
            "reuse_existing_blend": bool(args.reuse_existing_blend),
            "registry_managed_externally": bool(
                args.registry_managed_externally
            ),
            "producer_refresh_proof_json": str(
                args.producer_refresh_proof_json
            ),
            "quality": str(args.quality),
            "plate_mode": str(args.plate_mode),
        })
        mapped_targets = load_target_map(args.target_map_json)
        validate_target_paths(args.spm, mapped_targets)
        if mapped_targets and (not args.build_spm or not args.spm):
            raise RuntimeError(
                "--target-map-json은 --build-spm 및 동일한 --spm 목록과 함께 사용해야 함")
        producer_refresh_proof = None
        canonical_manifest_path = None
        if args.producer_refresh_proof_json:
            from atlas_producer_rebind import (
                validate_atlas_producer_rebind_proof,
            )
            proof_path = Path(args.producer_refresh_proof_json)
            producer_refresh_proof = validate_atlas_producer_rebind_proof(
                json.loads(proof_path.read_text(encoding="utf-8"))
            )
            canonical = producer_refresh_proof["canonical_spm"]["path"]
            if (
                not args.registry_managed_externally
                or not args.reuse_existing_blend
                or not args.build_spm
                or mapped_targets
                or len(args.spm) != 1
                or bool(args.work_dir)
                or str(Path(args.spm[0]).resolve(strict=False)).casefold()
                != str(Path(canonical).resolve(strict=False)).casefold()
                or producer_refresh_proof["producer"]["connection_mode"]
                != "assets_only"
            ):
                raise RuntimeError(
                    "producer refresh requires one exact assets-only canonical SPM "
                    "with externally managed registry"
                )
            canonical_path = Path(canonical)
            canonical_manifest_path = (
                canonical_path.parent
                / ".atlas_leaf_speedtree_targets"
                / f"{canonical_path.stem}.json"
            )
        blend_out = Path(args.blend_out)
        if args.reuse_existing_blend:
            if not blend_out.is_file():
                raise RuntimeError(f"재사용할 blend 없음: {blend_out}")
            blend_backup = backup_existing_blend(blend_out)
            bpy.ops.wm.open_mainfile(filepath=str(blend_out))
        elif blend_out.exists():
            raise RuntimeError(
                f"기존 blend 덮어쓰기 차단: {blend_out} (재사용 모드를 사용하세요)")

        enabled = addon_utils.enable("atlas_leaf_mesh_builder", default_set=False, persistent=False)
        if enabled is None:
            raise RuntimeError("애드온 atlas_leaf_mesh_builder 활성화 실패 (Blender addons 설치 확인)")

        props = bpy.context.scene.atlas_leaf_builder
        from atlas_leaf_mesh_builder.props import (
            add_spm_target_item,
            save_spm_target_registry,
            sync_spm_target_registry,
        )
        from atlas_leaf_mesh_builder.source_index import (
            current_blend_source_index,
        )
        if producer_refresh_proof is not None:
            # The direct staged exporter receives canonical.parent explicitly;
            # do not create or touch the legacy blend-side _atlas_job_work.
            work_dir = str(canonical_path.parent)
        else:
            work_dir = args.work_dir or str(
                Path(args.blend_out).parent / "_atlas_job_work"
            )
            Path(work_dir).mkdir(parents=True, exist_ok=True)
        props.output_dir = work_dir
        props.quality = args.quality
        props.surface_mode = args.plate_mode
        props.speedtree_material_name = args.material_name
        if args.reuse_existing_blend:
            collection = mesh_collection_for_reuse(props, args.material_name)
            props.collection_name = collection.name
        else:
            props.albedo_path = args.albedo
            props.alpha_path = args.alpha
            props.collection_name = args.material_name
            props.clear_existing = True
            # pair 비우기 → 모든 알파 아일랜드 사용
            props.pair_items.clear()
            props.pair_json = "[]"
            result = bpy.ops.atlas_leaf.generate()
            if "FINISHED" not in result:
                raise RuntimeError(f"generate 실패: {result}")
            collection = bpy.data.collections.get(args.material_name)
        mesh_count = len(collection_mesh_objects(collection)) if collection else 0
        if mesh_count == 0:
            raise RuntimeError("사용할 잎 메시가 없음 (알파 아일랜드/기존 Collection 확인)")

        if args.reuse_existing_blend and producer_refresh_proof is None:
            from atlas_leaf_mesh_builder.target_registry import load_target_registry
            if load_target_registry(blend_out) is not None:
                sync_spm_target_registry(props, initialize_missing=False)
        else:
            props.speedtree_spm_items.clear()
        if producer_refresh_proof is None:
            for spm in args.spm:
                add_spm_target_item(props, spm)

        blend_out.parent.mkdir(parents=True, exist_ok=True)
        if not args.reuse_existing_blend:
            bpy.ops.wm.save_as_mainfile(filepath=str(blend_out))
        if not args.registry_managed_externally:
            save_spm_target_registry(props)

        # In exact producer mode, finish every potentially fallible Blender
        # persistence/index step before the staged SPM transaction commits.
        # The transaction is then the final production mutation in this job.
        blend_source_index = None
        if producer_refresh_proof is not None:
            if bpy.data.is_dirty:
                bpy.ops.wm.save_mainfile()
            blend_source_index = current_blend_source_index(
                expected_blend_path=blend_out,
            )

        spm_summary = None
        target_results = []
        generator_connections_complete = None
        if args.build_spm and args.spm:
            if producer_refresh_proof is None:
                spm_backups = backup_spms(args.spm)
            if mapped_targets:
                target_results = apply_mapped_targets(
                    props, mapped_targets, args.material_name)
                generator_connections_complete = bool(target_results) and all(
                    row["generator_connection"].get("complete") is True
                    for row in target_results
                )
                if not generator_connections_complete:
                    raise RuntimeError("일부 최종 SK Generator 연결이 완료되지 않음")
                spm_summary = json.dumps(target_results, ensure_ascii=False)
            else:
                if producer_refresh_proof is not None:
                    exported = apply_exact_assets_only_target(
                        props,
                        args.spm[0],
                        args.material_name,
                        producer_refresh_proof,
                    )
                    spm_summary = json.dumps({
                        "spm": str(exported[0]),
                        "manifest": str(exported[1]),
                        "action": exported[3],
                        "material_id": exported[4],
                        "mesh_ids": list(exported[5]),
                    }, ensure_ascii=False)
                else:
                    # Legacy CLI compatibility: without a target map, retain
                    # the multi-target operator behavior.
                    spm_result = bpy.ops.atlas_leaf.build_speedtree_spm()
                    if "FINISHED" not in spm_result:
                        raise RuntimeError(f"SPM 반영 실패: {spm_result}")
                    spm_summary = props.last_report
        producer_refresh_receipt = None
        if producer_refresh_proof is not None:
            from atlas_producer_rebind import (
                validate_atlas_producer_refresh_receipt,
            )
            producer_refresh_receipt = (
                validate_atlas_producer_refresh_receipt(
                    producer_refresh_proof,
                    manifest_path=canonical_manifest_path,
                )
            )
        if blend_source_index is None:
            # Persist the exact datablocks that Blender will index. The
            # resulting SHA-bound row is the only source-image authority
            # consumed by PCG.
            if bpy.data.is_dirty:
                bpy.ops.wm.save_mainfile()
            blend_source_index = current_blend_source_index(
                expected_blend_path=blend_out,
            )

        report = {
            "status": "ok",
            "blend": str(blend_out),
            "meshes": mesh_count,
            "quality": args.quality,
            "plate_mode": args.plate_mode,
            "spm_targets": args.spm,
            "spm_built": bool(args.build_spm and args.spm),
            "spm_report": spm_summary,
            "target_results": target_results,
            "generator_connections_complete": generator_connections_complete,
            "blend_source_index": blend_source_index,
            "reused_existing_blend": bool(args.reuse_existing_blend),
            "blend_backup": str(blend_backup) if blend_backup else None,
            "spm_backups": [str(backup) for _target, backup in spm_backups],
            "producer_refresh_receipt": producer_refresh_receipt,
            "authority_sha256": authority.get(
                "parent_authority_sha256"
            ),
            "authority_unit": authority.get("unit_id"),
        }
    except Exception as exc:
        restored = []
        if spm_backups:
            try:
                restored = restore_spms(spm_backups)
            except Exception as restore_exc:
                restored.append(f"복원 실패: {restore_exc}")
        blend_restored = None
        if blend_backup:
            try:
                shutil.copy2(blend_backup, Path(args.blend_out))
                blend_restored = str(args.blend_out)
            except Exception as restore_exc:
                blend_restored = f"복원 실패: {restore_exc}"
        report = {
            "status": "error", "error": str(exc), "traceback": traceback.format_exc(),
            "authority_document_sha256": args.authority_sha256,
            "spm_backups": [str(backup) for _target, backup in spm_backups],
            "spm_restored": restored,
            "blend_backup": str(blend_backup) if blend_backup else None,
            "blend_restored": blend_restored,
        }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if report["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
