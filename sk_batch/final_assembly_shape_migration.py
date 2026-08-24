"""Migrate existing generated final Nanite Assemblies to Preserve Area.

Run this file through UnrealEditor-Cmd's PythonScript commandlet.  Targets are
discovered only from successful SK Batch Unreal reports, then verified as
native Nanite Assemblies with production provenance before they are changed.
NA_Base and constituent part assets are never included or modified.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from cluster_assembly_builder import (  # noqa: E402
    FINAL_ASSEMBLY_NANITE_SHAPE_PRESERVATION,
    _nanite_shape_preservation_preserve_area,
)

INPUT_REPORT_DIR_ENV = "SK_BATCH_FINAL_ASSEMBLY_REPORT_DIR"
OUTPUT_REPORT_ENV = "SK_BATCH_FINAL_ASSEMBLY_MIGRATION_REPORT"
REPORT_SCHEMA_VERSION = 1


def _package_path(value):
    return str(value or "").strip().split(".", 1)[0]


def discover_generated_final_assemblies(report_dir):
    """Return exact successful generated Assembly targets from report evidence."""
    report_dir = Path(report_dir)
    targets = {}
    rejected = []
    for path in sorted(report_dir.glob("*_unreal_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            rejected.append({"report": str(path), "reason": str(exc)})
            continue
        build = ((payload.get("cluster_assembly") or {}).get("build") or {})
        if payload.get("status") != "imported_ok" or build.get("status") != "ok":
            continue
        object_path = str(build.get("assembly") or "").strip()
        package_path = _package_path(object_path)
        asset_name = package_path.rsplit("/", 1)[-1]
        if not package_path.startswith("/Game/") or not asset_name.endswith(
            "_NaniteAssembly"
        ):
            rejected.append(
                {
                    "report": str(path),
                    "reason": "successful build has a non-canonical final Assembly path",
                    "assembly": object_path,
                }
            )
            continue
        row = targets.setdefault(
            package_path,
            {
                "asset_path": package_path,
                "object_path": object_path,
                "source_reports": [],
            },
        )
        row["source_reports"].append(str(path.resolve()))
    return {
        "targets": [targets[key] for key in sorted(targets)],
        "rejected_reports": rejected,
    }


def _assembly_overview(unreal, mesh):
    library = getattr(unreal, "NaniteAssemblyInspectorLibrary", None)
    if library is None:
        raise RuntimeError("NaniteAssemblyInspectorLibrary is unavailable")
    raw = library.get_skeletal_mesh_assembly_overview_json(mesh)
    try:
        overview = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Nanite Assembly overview returned invalid JSON") from exc
    if not overview.get("success"):
        raise RuntimeError(
            "asset is not a native Nanite Assembly: "
            + str(overview.get("error") or overview)
        )
    if not overview.get("provenance_present"):
        raise RuntimeError("generated Assembly production provenance is missing")
    return overview


def _migrate_target(unreal, target, preserve_area):
    started = time.perf_counter()
    asset_path = target["asset_path"]
    mesh = unreal.EditorAssetLibrary.load_asset(asset_path)
    if mesh is None or not isinstance(mesh, unreal.SkeletalMesh):
        raise RuntimeError("target is not a loadable SkeletalMesh")
    overview_before = _assembly_overview(unreal, mesh)
    nanite = mesh.get_editor_property("nanite_settings")
    before = nanite.get_editor_property("shape_preservation")
    changed = before != preserve_area
    rebuild_trigger = None
    checked_out = False
    if changed:
        subsystem = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)
        if not subsystem.checkout_asset(asset_path):
            raise RuntimeError("source-control checkout failed")
        checked_out = True
        modify = getattr(mesh, "modify", None)
        if callable(modify):
            modify()
        nanite.set_editor_property("shape_preservation", preserve_area)
        # Assigning the edited NaniteSettings struct is the UE Python
        # equivalent of the details-panel edit.  USkeletalMesh's property
        # setter synchronously invokes the Nanite/SkeletalMesh rebuild; this
        # object intentionally exposes neither post_edit_change nor a second
        # notify method in UE 5.8 Python.
        mesh.set_editor_property("nanite_settings", nanite)
        rebuild_trigger = "nanite_settings_property_assignment"
        if not unreal.EditorAssetLibrary.save_loaded_asset(
            mesh,
            only_if_is_dirty=False,
        ):
            raise RuntimeError("failed to save migrated final Assembly")
    after = (
        mesh.get_editor_property("nanite_settings")
        .get_editor_property("shape_preservation")
    )
    if after != preserve_area:
        raise RuntimeError("final Assembly did not retain Preserve Area")
    overview_after = _assembly_overview(unreal, mesh)
    for field in ("part_count", "instance_count"):
        if overview_after.get(field) != overview_before.get(field):
            raise RuntimeError(
                f"native Assembly {field} changed during shape migration"
            )
    return {
        "status": "migrated" if changed else "already_current",
        "asset_path": asset_path,
        "before": str(before),
        "after": str(after),
        "changed": changed,
        "checked_out": checked_out,
        "rebuild_trigger": rebuild_trigger,
        "part_count": overview_after.get("part_count"),
        "instance_count": overview_after.get("instance_count"),
        "provenance_present": overview_after.get("provenance_present"),
        "source_reports": target["source_reports"],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def run_migration(unreal, report_dir):
    discovery = discover_generated_final_assemblies(report_dir)
    if discovery["rejected_reports"]:
        raise RuntimeError(
            "invalid successful Assembly report evidence: "
            + json.dumps(discovery["rejected_reports"], ensure_ascii=False)
        )
    targets = discovery["targets"]
    if not targets:
        raise RuntimeError("no successful generated final Assemblies were discovered")
    preserve_area = _nanite_shape_preservation_preserve_area(unreal)
    if preserve_area is None:
        raise RuntimeError("UE 5.8 Preserve Area enum is unavailable")
    rows = []
    failures = []
    for index, target in enumerate(targets, start=1):
        unreal.log(
            f"[FinalAssemblyPreserveArea] {index}/{len(targets)} "
            f"{target['asset_path']}"
        )
        try:
            row = _migrate_target(unreal, target, preserve_area)
            rows.append(row)
            unreal.log(
                "[FinalAssemblyPreserveArea] "
                f"{row['status']} {row['asset_path']} "
                f"({row['elapsed_seconds']:.3f}s)"
            )
        except Exception as exc:
            failure = {
                "status": "failed",
                "asset_path": target["asset_path"],
                "error": str(exc),
                "source_reports": target["source_reports"],
            }
            failures.append(failure)
            unreal.log_error(
                "[FinalAssemblyPreserveArea] failed "
                f"{target['asset_path']}: {exc}"
            )
        finally:
            # Assembly assets retain their large constituent meshes while
            # loaded.  Release completed targets between rows so a full BAT
            # fleet migration does not accumulate every tree in one editor.
            system_library = getattr(unreal, "SystemLibrary", None)
            collect_garbage = getattr(system_library, "collect_garbage", None)
            if callable(collect_garbage):
                collect_garbage()
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "ok" if not failures else "failed",
        "policy": FINAL_ASSEMBLY_NANITE_SHAPE_PRESERVATION,
        "target_count": len(targets),
        "migrated_count": sum(row["status"] == "migrated" for row in rows),
        "already_current_count": sum(
            row["status"] == "already_current" for row in rows
        ),
        "failed_count": len(failures),
        "items": rows + failures,
    }


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main():
    import unreal

    report_dir = os.environ.get(INPUT_REPORT_DIR_ENV, "").strip()
    output_path = os.environ.get(OUTPUT_REPORT_ENV, "").strip()
    if not report_dir or not output_path:
        raise RuntimeError(
            f"{INPUT_REPORT_DIR_ENV} and {OUTPUT_REPORT_ENV} are required"
        )
    result = run_migration(unreal, report_dir)
    _atomic_write_json(output_path, result)
    if result["status"] != "ok":
        raise RuntimeError(
            f"final Assembly Preserve Area migration failed for "
            f"{result['failed_count']} asset(s)"
        )


if __name__ == "__main__":
    main()
