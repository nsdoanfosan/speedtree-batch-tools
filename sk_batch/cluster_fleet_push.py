"""Assembly and push every current production Cluster Assembly into Unreal.

Target selection comes only from manifests directly under
``<root>/<asset>/assembly``. Historical logs, staging copies, backups, and old
Unreal results never select work. A current pass-through manifest is selected
for live receipt revalidation as well, so a legacy pass-through overwrite can
recover from the latest Full FBX instead of orphaning an existing Assembly.
Exact normalized provider SPMs are assembled and pushed once, ahead of every
root that consumes them; each root is then rebuilt from the latest Full FBX.
Birch Paper is sorted last by default.
"""

from __future__ import annotations

import argparse
import json
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from exact_push import (
    DEFAULT_BLENDER,
    DEFAULT_UNREAL_EDITOR_CMD,
    DEFAULT_UNREAL_PROJECT,
    LOG_DIR,
    ExactPushError,
    build_exact_push_command,
    merge_unreal_result,
    run_headless_manifest,
)
from process_lifecycle import owned_run
from push_dependency_schedule import (
    PushDependencyError,
    cluster_dependency_spms,
    exact_dependency_contract_from_validated_manifest,
    normalized_path_key,
)
from sk_common import wind_preset_for_spm


DEFAULT_ROOT = Path(r"D:\OneDrive\Forestportfolio\02_nature\Tree")
ASSEMBLY_JOB = Path(__file__).resolve().parent / "jobs" / "assembly_headless_job.py"
PCG_AUDIT = (
    Path(__file__).resolve().parents[1]
    / "pcg_st9_texture_batch"
    / "pcg_texture_audit.py"
)
DEFAULT_P4_CLIENT = "UnrealProjects"


def _asset_package_file(unreal_project, asset_path):
    """Resolve a /Game package path to its existing project .uasset file."""
    package = str(asset_path or "").split(".", 1)[0].replace("\\", "/")
    if not package.casefold().startswith("/game/"):
        return None
    relative = package[len("/Game/") :]
    return Path(unreal_project).resolve().parent / "Content" / Path(
        *relative.split("/")
    ).with_suffix(".uasset")


def _is_read_only_file(path):
    details = path.stat()
    attributes = getattr(details, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 1))
    return not bool(details.st_mode & stat.S_IWUSR)


def checkout_headless_manifest_assets(
    manifest,
    unreal_project,
    *,
    p4_client=DEFAULT_P4_CLIENT,
    run_factory=subprocess.run,
):
    """Make every existing manifest package writable before headless Unreal.

    UnrealEditor-Cmd can report ``checkout_asset`` success while no source-control
    provider is active.  The subsequent package save then fails on Perforce's
    read-only file.  The fleet owns the immutable manifest already, so it can
    checkout that exact package set through the workspace client before launch.
    """
    existing = []
    seen = set()
    for item in manifest.get("items") or []:
        for asset_path in item.get("checkout_asset_paths") or []:
            package_file = _asset_package_file(unreal_project, asset_path)
            if package_file is None or not package_file.is_file():
                continue
            key = str(package_file).casefold()
            if key not in seen:
                seen.add(key)
                existing.append(package_file)

    read_only = [path for path in existing if _is_read_only_file(path)]
    checked_out = []
    for start in range(0, len(read_only), 64):
        chunk = read_only[start : start + 64]
        completed = run_factory(
            ["p4", "-c", str(p4_client), "edit", *map(str, chunk)],
            check=False,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ExactPushError(
                "Perforce checkout failed before headless Unreal"
                + (f": {detail}" if detail else "")
            )
        checked_out.extend(chunk)

    still_read_only = [path for path in read_only if _is_read_only_file(path)]
    if still_read_only:
        raise ExactPushError(
            "Perforce checkout returned success but packages remain read-only: "
            + ", ".join(map(str, still_read_only))
        )
    return {
        "client": str(p4_client),
        "existing": [str(path) for path in existing],
        "checked_out": [str(path) for path in checked_out],
    }


def build_receipt_refresh_command(target, report_path):
    """Refresh the exact target's current Assembly receipt from its live SPM."""
    spm = Path(target["spm"]).resolve()
    return [
        sys.executable,
        str(PCG_AUDIT),
        "--target",
        str(spm.parent),
        "--target-mesh",
        spm.stem,
        "--cluster-assembly-only",
        "--json",
        str(Path(report_path).resolve()),
    ]


def build_assembly_command(target, blender, material_contract, report_path):
    spm = Path(target["spm"]).resolve()
    return [
        str(Path(blender).resolve()),
        "--factory-startup",
        "-b",
        "--python",
        str(ASSEMBLY_JOB),
        "--",
        "--spm",
        str(spm),
        "--speedtree-spm",
        str(spm),
        "--blend",
        str(spm.with_suffix(".blend")),
        "--wind",
        wind_preset_for_spm(spm),
        "--material-contract",
        str(Path(material_contract).resolve()),
        "--report",
        str(Path(report_path).resolve()),
    ]


def validate_assembly_result(report_path, target):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assembly_manifest = report.get("cluster_assembly_manifest") or {}
    role_demotions = list(
        (report.get("cluster_assembly_handoff") or {}).get(
            "role_demotions"
        )
        or []
    )
    if (
        assembly_manifest.get("status") == "pass_through"
        and assembly_manifest.get("content_decision") == "pass_through"
    ):
        orphaned = assembly_manifest.get(
            "existing_assembly_assets_orphaned"
        )
        return {
            "ok": True,
            "pass_through": True,
            "problems": [],
            "policy": "current_receipt_pass_through",
            "parts": 0,
            "bindings": 0,
            "assigned": 0,
            "unmatched": 0,
            "preserved_role_polygons_removed": 0,
            "existing_assembly_assets_orphaned": orphaned,
            "report": str(Path(report_path).resolve()),
        }
    manifest_path = Path(target["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    placement = manifest.get("placement_contract") or {}
    attachment_bones = manifest.get("attachment_bone_contract") or {}
    base = manifest.get("base") or {}
    preserved = list(manifest.get("preserved_render_components") or [])
    parts = list(manifest.get("parts") or [])
    problems = []
    if report.get("status") != "ok":
        problems.append("assembly_not_ok")
    if (
        placement.get("version") != 2
        or placement.get("identity_policy")
        != "native_modeler_authored_position_receipt_v1"
        or placement.get("translation_source")
        != "native_modeler_runtime_receipt"
    ):
        problems.append("assembly_binding_policy_not_current")
    if any(
        key in placement
        for key in (
            "authored_node_assignment",
            "authored_node_match_threshold_meters",
            "claimed_authored_node_count",
            "degraded_authored_card_binding_count",
        )
    ):
        problems.append("legacy_attachment_identity_contract_present")
    placement_spm = placement.get("source_spm") or {}
    attachment_spm = attachment_bones.get("source_spm") or {}
    try:
        declared_attachment_bone_count = int(
            attachment_bones.get("bone_count") or 0
        )
        generated_instance_count = int(
            attachment_bones.get("generated_instance_count") or 0
        )
    except (TypeError, ValueError):
        declared_attachment_bone_count = -1
        generated_instance_count = -1
    native_receipt = attachment_bones.get("receipt") or {}
    if (
        attachment_bones.get("status") != "ready"
        or attachment_bones.get("policy")
        != "native_modeler_runtime_receipt_v1"
        or declared_attachment_bone_count <= 0
        or generated_instance_count <= 0
        or not native_receipt.get("sha256")
        or str(placement_spm.get("path") or "")
        != str(attachment_spm.get("path") or "")
        or str(placement_spm.get("sha256") or "").casefold()
        != str(attachment_spm.get("sha256") or "").casefold()
    ):
        problems.append("native_attachment_receipt_contract_missing")
    preserved_polygons = sum(
        int(row.get("polygon_count") or 0) for row in preserved
    )
    removed_preserved = int(
        base.get("unmatched_role_components_removed_from_base") or 0
    )
    if removed_preserved != 0:
        problems.append(
            "preserved_role_geometry_deleted_from_base:"
            + str(removed_preserved)
        )
    binding_count = sum(
        len(part.get("bindings") or []) for part in parts
    )
    if not parts or binding_count <= 0:
        problems.append("assembly_manifest_has_no_parts_or_bindings")
    exact_binding_count = int(
        placement.get("exact_render_attachment_binding_count") or 0
    )
    if exact_binding_count != binding_count:
        problems.append(
            "exact_attachment_binding_count_mismatch:"
            f"{exact_binding_count}!={binding_count}"
        )
    return {
        "ok": not problems,
        "problems": problems,
        "policy": placement.get("identity_policy"),
        "parts": len(parts),
        "bindings": binding_count,
        "assigned": exact_binding_count,
        "unmatched": 0,
        "preserved_role_polygons_removed": removed_preserved,
        "preserved_role_polygons_kept": preserved_polygons,
        "role_demotions": role_demotions,
        "report": str(Path(report_path).resolve()),
    }


def validate_provider_assembly_result(report_path):
    """Validate the normalized provider artifact produced by Assembly."""
    report_path = Path(report_path).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    problems = []
    if report.get("status") != "ok":
        problems.append("provider_assembly_not_ok")
    pipeline_path = Path(str(report.get("pipeline_report") or ""))
    try:
        pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        pipeline = {}
        problems.append(f"provider_pipeline_report_unreadable:{exc}")
    source = pipeline.get("cluster_source_build_contract") or {}
    postcondition = pipeline.get("assembly_export_postcondition") or {}
    objects = list(postcondition.get("objects") or [])
    if pipeline and pipeline.get("status") != "done":
        problems.append("provider_pipeline_not_done")
    assembled_blend = Path(str(report.get("blend") or ""))
    if report.get("unreal_push_ready") is not True:
        problems.append("provider_unreal_push_not_ready")
    if not assembled_blend.is_file():
        problems.append("provider_assembled_blend_missing")
    if not objects:
        problems.append("provider_export_postcondition_has_no_objects")
    final_consolidation = next(
        (
            row
            for row in (pipeline.get("steps") or [])
            if row.get("name") == "consolidate_speedtree_group_materials"
        ),
        {},
    )
    import_consolidation = (
        (pipeline.get("import") or {}).get("material_consolidation") or {}
    )
    material_consolidation = (
        import_consolidation
        if import_consolidation.get("status") == "applied"
        else final_consolidation
    )
    return {
        "ok": not problems,
        "problems": problems,
        "report": str(report_path),
        "pipeline_report": str(pipeline_path),
        "assembled_blend": str(assembled_blend),
        "cluster_source_mode": source.get("mode"),
        "source_object": source.get("source_object"),
        "export_objects": [str(row.get("name") or "") for row in objects],
        "material_consolidation": material_consolidation,
    }


def validate_provider_live_result(report):
    unreal_result = report.get("unreal_result") or {}
    problems = []
    if report.get("status") != "ok":
        problems.append("provider_push_not_ok")
    if unreal_result.get("status") != "imported_ok":
        problems.append("provider_unreal_import_not_ok")
    asset_paths = list(unreal_result.get("asset_paths") or [])
    material_mesh = str(
        (unreal_result.get("materials") or {}).get("mesh") or ""
    ).strip()
    if material_mesh and material_mesh not in asset_paths:
        asset_paths.append(material_mesh)
    return {
        "ok": not problems,
        "problems": problems,
        "asset_paths": asset_paths,
        "materials": unreal_result.get("materials"),
    }


def discover_provider_dependencies(targets):
    """Resolve exact normalized Cluster sources before rebuilding roots."""
    ordered = []
    seen = set()
    by_root = {}
    issues = {}
    resolution = {}
    for target in targets:
        root_spm = Path(target["spm"]).resolve()
        try:
            dependencies = cluster_dependency_spms(root_spm)
        except PushDependencyError as exc:
            # Updating one shared provider makes sibling roots' saved artifact
            # fingerprints stale until those roots are rebuilt.  Their old
            # manifest may still seed *execution order* from exact recorded
            # source_blend paths; it never decides role presence or geometry.
            # The post-Assembly manifest below is rebuilt from the latest Full
            # FBX, re-resolved, and any newly required provider is processed
            # before that root is exported.
            try:
                manifest = json.loads(
                    Path(target["manifest"]).read_text(encoding="utf-8")
                )
                if (
                    manifest.get("status") != "ready"
                    or manifest.get("content_decision") != "build"
                ):
                    raise ValueError("no saved build dependency hints")
                dependencies = []
                for part in manifest.get("parts") or []:
                    source_blend = (
                        (part.get("external_source") or {}).get(
                            "source_blend"
                        )
                        or {}
                    )
                    path = Path(str(source_blend.get("path") or ""))
                    dependency = path.with_suffix(".spm")
                    if not path.name or not dependency.is_file():
                        raise ValueError(
                            "saved provider dependency path is missing"
                        )
                    dependencies.append(dependency)
                resolution[str(root_spm)] = {
                    "policy": "saved_manifest_execution_hint_only",
                    "reason": str(exc),
                }
            except (OSError, ValueError, TypeError) as hint_exc:
                by_root[str(root_spm)] = []
                issues[str(root_spm)] = (
                    f"{exc}; execution hint unavailable: {hint_exc}"
                )
                continue
        else:
            resolution[str(root_spm)] = {
                "policy": "current_validated_manifest_or_relation",
            }
        rows = []
        for dependency in dependencies:
            dependency = Path(dependency).resolve()
            rows.append(str(dependency))
            key = normalized_path_key(dependency)
            if key not in seen:
                seen.add(key)
                ordered.append(dependency)
        by_root[str(root_spm)] = rows
    return ordered, by_root, issues, resolution


def discover_current_cluster_targets(
    root: Path, only: list[str] | None = None
) -> tuple[list[dict], list[dict]]:
    root = root.expanduser().resolve()
    filters = [str(value).casefold() for value in (only or []) if str(value)]
    targets = []
    missing = []
    for manifest_path in root.glob("*/assembly/*_cluster_assembly_bindings.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            missing.append({
                "manifest": str(manifest_path),
                "reason": f"unreadable_manifest: {exc}",
            })
            continue
        ready_build = (
            manifest.get("status") == "ready"
            and manifest.get("content_decision") == "build"
        )
        current_pass_through = (
            manifest.get("status") == "pass_through"
            and manifest.get("content_decision") == "pass_through"
        )
        if not ready_build and not current_pass_through:
            continue
        pass_through_spm = str(
            ((manifest.get("pass_through_provenance") or {}).get(
                "requested_spm"
            ) or "")
        ).strip()
        stem = str(manifest.get("full_asset_stem") or "").strip()
        if current_pass_through and pass_through_spm:
            stem = Path(pass_through_spm).stem
        if current_pass_through and not stem:
            suffix = "_cluster_assembly_bindings"
            stem = (
                manifest_path.stem[:-len(suffix)]
                if manifest_path.stem.endswith(suffix)
                else ""
            )
        parts = list(manifest.get("parts") or [])
        if not stem or (ready_build and not parts):
            missing.append({
                "manifest": str(manifest_path),
                "stem": stem,
                "diagnostic": (
                    "ready_build_manifest_has_no_parts_or_stem"
                    if ready_build
                    else "pass_through_manifest_has_no_stem"
                ),
            })
            continue
        asset_dir = manifest_path.parents[1]
        spm = (
            Path(pass_through_spm)
            if current_pass_through and pass_through_spm
            else asset_dir / f"{stem}.spm"
        ).expanduser().resolve()
        if spm.parent != asset_dir.resolve():
            missing.append({
                "manifest": str(manifest_path),
                "stem": stem,
                "diagnostic": "current_manifest_spm_outside_asset_directory",
                "spm": str(spm),
            })
            continue
        declared_wind_json = str(
            ((manifest.get("wind_contract") or {}).get("wind_json") or {}).get(
                "path"
            )
            or ""
        ).strip()
        wind_json = (
            Path(declared_wind_json)
            if declared_wind_json
            else asset_dir
            / "JSON"
            / f"{stem}_dynamic_wind_import_from_megaplant_groups.json"
        )
        required = {
            "spm": spm,
            "blend": spm.with_suffix(".blend"),
            "wind_json": wind_json,
        }
        absent = [name for name, path in required.items() if not str(path) or not path.is_file()]
        if absent:
            missing.append({
                "manifest": str(manifest_path),
                "stem": stem,
                "diagnostic": "missing_required_current_data",
                "missing": absent,
                "paths": {name: str(path) for name, path in required.items()},
            })
            continue
        targets.append({
            "stem": stem,
            "spm": spm.resolve(),
            "manifest": manifest_path.resolve(),
            "expected_parts": len(parts),
            "expected_bindings": sum(len(part.get("bindings") or []) for part in parts),
            "birch_paper": "birch_paper" in stem.casefold(),
            "selection_policy": (
                "ready_build_manifest"
                if ready_build
                else "current_pass_through_receipt_revalidation"
            ),
        })

    # An explicitly requested current SPM may legitimately have no production
    # manifest yet (for example a sibling tree that has never received its
    # first Assembly build).  The explicit request selects the current source
    # for live receipt revalidation; the latest Full FBX still decides whether
    # the result is build or pass-through.
    selected_stems = {row["stem"].casefold() for row in targets}
    if filters:
        for spm in root.glob("*/*.spm"):
            stem = spm.stem
            if stem.casefold() in selected_stems:
                continue
            if not any(value in stem.casefold() for value in filters):
                continue
            asset_dir = spm.parent
            wind_json = (
                asset_dir
                / "JSON"
                / f"{stem}_dynamic_wind_import_from_megaplant_groups.json"
            )
            required = {
                "spm": spm,
                "blend": spm.with_suffix(".blend"),
                "wind_json": wind_json,
            }
            absent = [
                name for name, path in required.items() if not path.is_file()
            ]
            manifest_path = (
                asset_dir
                / "assembly"
                / f"{stem}_cluster_assembly_bindings.json"
            )
            if absent:
                missing.append({
                    "manifest": str(manifest_path),
                    "stem": stem,
                    "diagnostic": "missing_required_explicit_target_data",
                    "missing": absent,
                    "paths": {
                        name: str(path) for name, path in required.items()
                    },
                })
                continue
            targets.append({
                "stem": stem,
                "spm": spm.resolve(),
                "manifest": manifest_path.resolve(),
                "expected_parts": 0,
                "expected_bindings": 0,
                "birch_paper": "birch_paper" in stem.casefold(),
                "selection_policy": "explicit_current_spm_revalidation",
            })
            selected_stems.add(stem.casefold())
    targets.sort(key=lambda row: (row["birch_paper"], row["stem"].casefold()))
    return targets, missing


def validate_live_result(report: dict, target: dict) -> dict:
    unreal_result = report.get("unreal_result") or {}
    assembly = unreal_result.get("cluster_assembly") or {}
    build = assembly.get("build") or {}
    built_parts = list(build.get("parts") or [])
    wind = build.get("dynamic_wind") or {}
    provenance = build.get("provenance") or {}
    problems = []
    if report.get("status") != "ok" or unreal_result.get("status") != "imported_ok":
        problems.append("unreal_import_not_ok")
    if build.get("status") != "ok":
        problems.append("assembly_build_not_ok")
    if len(built_parts) != target["expected_parts"]:
        problems.append(
            f"part_count:{len(built_parts)}!={target['expected_parts']}"
        )
    if int(build.get("binding_count") or 0) != target["expected_bindings"]:
        problems.append(
            f"binding_count:{int(build.get('binding_count') or 0)}!={target['expected_bindings']}"
        )
    if not built_parts or any(int(row.get("bindings") or 0) <= 0 for row in built_parts):
        problems.append("one_or_more_3d_parts_have_no_bindings")
    if wind.get("success") is not True:
        problems.append("assembly_wind_import_failed")
    if provenance.get("success") is not True:
        problems.append("assembly_provenance_missing")
    return {
        "ok": not problems,
        "problems": problems,
        "assembly": build.get("assembly"),
        "built_parts": len(built_parts),
        "binding_count": int(build.get("binding_count") or 0),
        "wind_success": wind.get("success") is True,
        "provenance_success": provenance.get("success") is True,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Push and verify all current production Cluster Assemblies",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--unreal-project", type=Path, default=DEFAULT_UNREAL_PROJECT)
    parser.add_argument("--p4-client", default=DEFAULT_P4_CLIENT)
    parser.add_argument(
        "--unreal-editor-cmd",
        type=Path,
        default=DEFAULT_UNREAL_EDITOR_CMD,
    )
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--skip-birch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    targets, missing = discover_current_cluster_targets(
        args.root, only=args.only
    )
    filters = [value.casefold() for value in args.only]
    if filters:
        targets = [
            row for row in targets
            if any(value in row["stem"].casefold() for value in filters)
        ]
    if args.skip_birch:
        targets = [row for row in targets if not row["birch_paper"]]
    excluded_filters = [value.casefold() for value in args.exclude]
    excluded_targets = [
        row for row in targets
        if any(value in row["stem"].casefold() for value in excluded_filters)
    ]
    if excluded_filters:
        targets = [
            row for row in targets
            if not any(
                value in row["stem"].casefold()
                for value in excluded_filters
            )
        ]

    (
        provider_spms,
        dependencies_by_root,
        dependency_issues,
        dependency_resolution,
    ) = discover_provider_dependencies(targets)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    fleet_report_path = args.log_dir / f"cluster_fleet_push_{run_id}.json"
    fleet = {
        "status": "running",
        "root": str(args.root.expanduser().resolve()),
        "transport": "headless_commandlet",
        "assembly_policy": (
            "provider_assembly_push_then_refresh_exact_target_"
            "assembly_export_and_push"
        ),
        "birch_paper_order": "last",
        "targets": [str(row["spm"]) for row in targets],
        "excluded_targets": [str(row["spm"]) for row in excluded_targets],
        "provider_dependencies": [str(path) for path in provider_spms],
        "dependencies_by_root": dependencies_by_root,
        "dependency_issues": dependency_issues,
        "dependency_resolution": dependency_resolution,
        "missing_current_assembly_data": missing,
        "provider_results": [],
        "results": [],
    }
    args.log_dir.mkdir(parents=True, exist_ok=True)
    def save_fleet():
        fleet_report_path.write_text(
            json.dumps(fleet, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    save_fleet()
    print(f"SK_CLUSTER_FLEET_TARGETS={len(targets)}")
    print(f"SK_CLUSTER_FLEET_PROVIDERS={len(provider_spms)}")
    print(f"SK_CLUSTER_FLEET_REPORT={fleet_report_path}")
    if args.dry_run:
        fleet["status"] = "dry_run"
        save_fleet()
        return 0

    pending = []
    processed_providers = {}

    def process_provider(provider_spm, dependency_of=None):
        provider_spm = Path(provider_spm).resolve()
        key = normalized_path_key(provider_spm)
        existing = processed_providers.get(key)
        if existing is not None:
            if dependency_of:
                roots = existing.setdefault("dependency_of", [])
                if dependency_of not in roots:
                    roots.append(dependency_of)
            return existing

        ordinal = len(processed_providers) + 1
        result = {
            "stem": provider_spm.stem,
            "spm": str(provider_spm),
            "dependency_of": [dependency_of] if dependency_of else [],
            "status": "running",
        }
        processed_providers[key] = result
        fleet["provider_results"].append(result)
        save_fleet()
        print(
            f"[provider {ordinal}] ASSEMBLY/EXPORT {provider_spm.stem}",
            flush=True,
        )
        try:
            command, outputs = build_exact_push_command(
                provider_spm,
                blender=args.blender,
                log_dir=args.log_dir,
                run_id=f"fleet_{run_id}_provider_{ordinal:03d}",
                unreal_project=args.unreal_project,
            )
            assembly_report = (
                args.log_dir
                / (
                    f"{provider_spm.stem}_fleet_provider_assembly_"
                    f"{run_id}_{ordinal:03d}.json"
                )
            )
            assembly_command = build_assembly_command(
                {"spm": provider_spm},
                args.blender,
                outputs["material_contract"],
                assembly_report,
            )
            result["assembly_report"] = str(assembly_report)
            assembly_completed = owned_run(
                assembly_command,
                source=(
                    "sk_batch.cluster_fleet_push.provider_blender_assembly"
                ),
                run_factory=subprocess.run,
                check=False,
            )
            result["assembly_returncode"] = assembly_completed.returncode
            if assembly_completed.returncode:
                raise RuntimeError(
                    "provider Assembly exited "
                    f"{assembly_completed.returncode}"
                )
            verification = validate_provider_assembly_result(assembly_report)
            result["assembly_verification"] = verification
            if not verification["ok"]:
                raise RuntimeError(
                    "provider Assembly postcondition failed: "
                    + "; ".join(verification["problems"])
                )
            completed = owned_run(
                command,
                source=(
                    "sk_batch.cluster_fleet_push.provider_blender_export"
                ),
                run_factory=subprocess.run,
                check=False,
            )
            result["returncode"] = completed.returncode
            result["report"] = str(outputs["report"])
            if completed.returncode:
                raise RuntimeError(
                    "provider production export exited "
                    f"{completed.returncode}"
                )
            export_report = json.loads(
                outputs["report"].read_text(encoding="utf-8")
            )
            if export_report.get("status") != "exported_pending_unreal":
                raise RuntimeError(
                    "provider export did not reach exported_pending_unreal"
                )
            exported_manifest = json.loads(
                outputs["manifest"].read_text(encoding="utf-8")
            )
            items = list(exported_manifest.get("items") or [])
            if len(items) != 1:
                raise RuntimeError(
                    "provider exact export manifest item count is "
                    f"{len(items)}, expected 1"
                )
            result["status"] = "exported_pending_unreal"
            pending.append({
                "kind": "provider",
                "outputs": outputs,
                "item": items[0],
                "result": result,
            })
        except (ExactPushError, OSError, ValueError, RuntimeError) as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
        save_fleet()
        return result

    roots_by_provider = {}
    for root_spm, dependencies in dependencies_by_root.items():
        for provider_spm in dependencies:
            roots_by_provider.setdefault(
                normalized_path_key(provider_spm), []
            ).append(root_spm)
    for provider_spm in provider_spms:
        roots = roots_by_provider.get(normalized_path_key(provider_spm), [])
        first_root = roots[0] if roots else None
        provider_result = process_provider(provider_spm, first_root)
        for root_spm in roots[1:]:
            if root_spm not in provider_result["dependency_of"]:
                provider_result["dependency_of"].append(root_spm)
    save_fleet()

    for index, target in enumerate(targets, 1):
        print(f"[{index}/{len(targets)}] EXPORT {target['stem']}", flush=True)
        result = {
            "stem": target["stem"],
            "spm": str(target["spm"]),
            "manifest": str(target["manifest"]),
        }
        fleet["results"].append(result)
        try:
            root_key = str(Path(target["spm"]).resolve())
            dependency_issue = dependency_issues.get(root_key)
            if dependency_issue:
                raise RuntimeError(
                    "provider dependency discovery failed: "
                    + dependency_issue
                )
            failed_dependencies = [
                row["spm"]
                for row in fleet["provider_results"]
                if row.get("status") == "failed"
                and root_key in row.get("dependency_of", [])
            ]
            if failed_dependencies:
                raise RuntimeError(
                    "provider dependency failed: "
                    + ", ".join(failed_dependencies)
                )
            receipt_refresh_report = (
                args.log_dir
                / (
                    f"{target['stem']}_fleet_receipt_refresh_"
                    f"{run_id}_{index:03d}.json"
                )
            )
            receipt_refresh_command = build_receipt_refresh_command(
                target,
                receipt_refresh_report,
            )
            result["receipt_refresh_report"] = str(receipt_refresh_report)
            receipt_refresh_completed = owned_run(
                receipt_refresh_command,
                source=(
                    "sk_batch.cluster_fleet_push.cluster_receipt_refresh"
                ),
                run_factory=subprocess.run,
                check=False,
            )
            result["receipt_refresh_returncode"] = (
                receipt_refresh_completed.returncode
            )
            if receipt_refresh_completed.returncode:
                raise RuntimeError(
                    "current SPM Assembly receipt refresh exited "
                    f"{receipt_refresh_completed.returncode}"
                )
            command, outputs = build_exact_push_command(
                target["spm"],
                blender=args.blender,
                log_dir=args.log_dir,
                run_id=f"fleet_{run_id}_{index:03d}",
                unreal_project=args.unreal_project,
            )
            assembly_report = (
                args.log_dir
                / f"{target['stem']}_fleet_assembly_{run_id}_{index:03d}.json"
            )
            assembly_command = build_assembly_command(
                target,
                args.blender,
                outputs["material_contract"],
                assembly_report,
            )
            result["assembly_report"] = str(assembly_report)
            while True:
                assembly_completed = owned_run(
                    assembly_command,
                    source="sk_batch.cluster_fleet_push.blender_assembly",
                    run_factory=subprocess.run,
                    check=False,
                )
                result["assembly_returncode"] = assembly_completed.returncode
                if assembly_completed.returncode:
                    raise RuntimeError(
                        "production Assembly exited "
                        f"{assembly_completed.returncode}"
                    )
                assembly_verification = validate_assembly_result(
                    assembly_report,
                    target,
                )
                result["assembly_verification"] = assembly_verification
                if not assembly_verification["ok"]:
                    raise RuntimeError(
                        "production Assembly postcondition failed: "
                        + "; ".join(assembly_verification["problems"])
                    )
                if assembly_verification.get("pass_through"):
                    break
                assembly_payload = json.loads(
                    assembly_report.read_text(encoding="utf-8")
                )
                dependency_contract = (
                    exact_dependency_contract_from_validated_manifest(
                        target["spm"],
                        assembly_payload.get("cluster_assembly_manifest"),
                    )
                )
                current_dependencies = list(
                    dependency_contract.get("dependency_spms") or []
                )
                result["validated_provider_dependencies"] = (
                    current_dependencies
                )
                newly_processed = False
                for provider_spm in current_dependencies:
                    provider_key = normalized_path_key(provider_spm)
                    if provider_key not in processed_providers:
                        newly_processed = True
                    provider_result = process_provider(
                        provider_spm,
                        root_key,
                    )
                    if provider_result.get("status") == "failed":
                        raise RuntimeError(
                            "provider dependency failed: "
                            + str(provider_spm)
                        )
                if not newly_processed:
                    break
                result["assembly_repeated_after_new_provider"] = True
            if assembly_verification.get("pass_through"):
                result["status"] = "skipped_no_current_assembly"
                result["diagnostic"] = (
                    "current receipt declares Assembly pass-through; it was "
                    "recorded separately when a production build manifest "
                    "already existed, and no Assembly was exported"
                )
                save_fleet()
                continue
            target["expected_parts"] = assembly_verification["parts"]
            target["expected_bindings"] = assembly_verification["bindings"]
            completed = owned_run(
                command,
                source="sk_batch.cluster_fleet_push.blender_export",
                run_factory=subprocess.run,
                check=False,
            )
            result["returncode"] = completed.returncode
            result["report"] = str(outputs["report"])
            if completed.returncode:
                raise RuntimeError(f"production export exited {completed.returncode}")
            export_report = json.loads(
                outputs["report"].read_text(encoding="utf-8")
            )
            if export_report.get("status") != "exported_pending_unreal":
                raise RuntimeError(
                    "production export did not reach exported_pending_unreal"
                )
            exported_manifest = json.loads(
                outputs["manifest"].read_text(encoding="utf-8")
            )
            items = list(exported_manifest.get("items") or [])
            if len(items) != 1:
                raise RuntimeError(
                    f"exact export manifest item count is {len(items)}, expected 1"
                )
            result["status"] = "exported_pending_unreal"
            pending.append({
                "kind": "root",
                "target": target,
                "outputs": outputs,
                "item": items[0],
                "result": result,
            })
        except (ExactPushError, OSError, ValueError, RuntimeError) as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
        save_fleet()

    if pending:
        manifest_path = args.log_dir / f"cluster_fleet_push_{run_id}_unreal_manifest.json"
        checkpoint_path = args.log_dir / f"cluster_fleet_push_{run_id}_unreal_checkpoint.json"
        batch_report_path = args.log_dir / f"cluster_fleet_push_{run_id}_unreal_report.json"
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "checkpoint_path": str(checkpoint_path.resolve()),
            "report_path": str(batch_report_path.resolve()),
            "max_item_crash_retries": 2,
            "items": [entry["item"] for entry in pending],
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        fleet["unreal_manifest"] = str(manifest_path)
        fleet["unreal_checkpoint"] = str(checkpoint_path)
        fleet["unreal_report"] = str(batch_report_path)
        save_fleet()
        try:
            fleet["pre_unreal_checkout"] = checkout_headless_manifest_assets(
                manifest,
                args.unreal_project,
                p4_client=args.p4_client,
            )
            save_fleet()
            batch_result = run_headless_manifest(
                manifest_path,
                checkpoint_path,
                batch_report_path,
                unreal_project=args.unreal_project,
                unreal_editor_cmd=args.unreal_editor_cmd,
            )
            for entry in pending:
                result = entry["result"]
                report = merge_unreal_result(entry["outputs"], batch_result)
                verification = (
                    validate_provider_live_result(report)
                    if entry["kind"] == "provider"
                    else validate_live_result(report, entry["target"])
                )
                result["verification"] = verification
                if verification["ok"]:
                    result["status"] = (
                        "verified_dependency_in_unreal"
                        if entry["kind"] == "provider"
                        else "verified_in_unreal"
                    )
                else:
                    result["status"] = "failed"
                    result["error"] = "; ".join(verification["problems"])
        except (ExactPushError, OSError, ValueError, RuntimeError) as exc:
            for entry in pending:
                result = entry["result"]
                if result["status"] == "exported_pending_unreal":
                    result["status"] = "failed"
                    result["error"] = str(exc)
        save_fleet()

    successful_statuses = {
        "verified_in_unreal",
        "skipped_no_current_assembly",
    }
    failed = [
        row for row in fleet["results"]
        if row["status"] not in successful_statuses
    ]
    failed_providers = [
        row for row in fleet["provider_results"]
        if row["status"] != "verified_dependency_in_unreal"
    ]
    fleet["status"] = (
        "ok" if not failed and not failed_providers else "failed"
    )
    fleet["verified_count"] = len([
        row for row in fleet["results"]
        if row["status"] == "verified_in_unreal"
    ])
    fleet["skipped_no_current_assembly_count"] = len([
        row for row in fleet["results"]
        if row["status"] == "skipped_no_current_assembly"
    ])
    fleet["failed_count"] = len(failed)
    fleet["provider_verified_count"] = len([
        row for row in fleet["provider_results"]
        if row["status"] == "verified_dependency_in_unreal"
    ])
    fleet["provider_failed_count"] = len(failed_providers)
    save_fleet()
    print(f"SK_CLUSTER_FLEET_VERIFIED={fleet['verified_count']}")
    print(
        "SK_CLUSTER_FLEET_SKIPPED_NO_CURRENT_ASSEMBLY="
        f"{fleet['skipped_no_current_assembly_count']}"
    )
    print(f"SK_CLUSTER_FLEET_FAILED={fleet['failed_count']}")
    print(
        "SK_CLUSTER_FLEET_PROVIDER_VERIFIED="
        f"{fleet['provider_verified_count']}"
    )
    print(
        "SK_CLUSTER_FLEET_PROVIDER_FAILED="
        f"{fleet['provider_failed_count']}"
    )
    return 0 if not failed and not failed_providers else 1


if __name__ == "__main__":
    raise SystemExit(main())
