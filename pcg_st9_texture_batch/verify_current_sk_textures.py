"""Verify every current SK SPM against the normalized SpeedTree texture contract."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pcg_texture_audit import (
    active_material_ids,
    canonical_material_name,
    is_backup_path,
    preserved_cluster_materials,
    read_maybe_gzip_text,
)
from pcg_texture_common import REPORT_DIR, load_config
from spm_texture_normalize import inspect_material_slots


EXPECTED_SLOTS = {
    "color", "opacity", "normal", "gloss",
    "subsurfacecolor", "subsurfaceamount", "ao", "height",
}


def resolve_ref(spm, value):
    return (Path(spm).parent / str(value).replace("/", "\\")).resolve()


def verify(cfg=None):
    cfg = cfg or load_config()
    root = Path(cfg["tree_root"])
    spms = sorted({
        path.resolve()
        for folder in root.iterdir() if folder.is_dir() and folder.name != "_spm_backups"
        for path in folder.glob("SK_*.spm")
        if path.is_file() and not is_backup_path(path)
    }, key=lambda path: str(path).lower())

    errors = []
    texture_sets = {}
    no_material_property = []
    material_count = 0
    subsurface_enabled = 0
    subsurface_disabled = 0
    preserved_cluster_count = 0
    preserved_by_spm = {}
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name == "_spm_backups":
            continue
        for row in preserved_cluster_materials(folder):
            preserved_by_spm.setdefault(str(Path(row["spm"]).resolve()).lower(), {})[
                canonical_material_name(row["material_name"]).lower()
            ] = row

    for spm in spms:
        rows = inspect_material_slots(read_maybe_gzip_text(spm))
        active = {str(value).lower() for value in active_material_ids(spm)} - {"0"}
        if not active:
            active = set(rows)
            no_material_property.append(str(spm))
        for material_id in active:
            row = rows.get(material_id)
            if not row:
                errors.append(f"{spm.name}: material ID {material_id} is missing")
                continue
            material_count += 1
            name = row["name"]
            slots = row["slots"]
            label = f"{spm.name}/{name}"
            preserved = preserved_by_spm.get(str(spm.resolve()).lower(), {}).get(
                canonical_material_name(name).lower())
            if preserved:
                source_rows = inspect_material_slots(
                    read_maybe_gzip_text(preserved["source_spm"]))
                source = next((candidate for candidate in source_rows.values()
                               if canonical_material_name(candidate["name"]).lower()
                               == canonical_material_name(name).lower()), None)
                if not source or slots != source["slots"]:
                    errors.append(f"{label}: cluster-render slots differ from source SPM")
                preserved_cluster_count += 1
                continue
            if set(slots) != EXPECTED_SLOTS:
                errors.append(f"{label}: unexpected slots {sorted(slots)}")
                continue

            checks = (
                ("gloss", "source", "2"),
                ("gloss", "invert", "true"),
                ("ao", "source", "1"),
                ("opacity", "enabled", "false"),
                ("opacity", "color_x", "1"),
                ("subsurfaceamount", "enabled", "false"),
            )
            for slot, field, expected in checks:
                if slots[slot][field] != expected:
                    errors.append(
                        f"{label}: {slot}.{field}={slots[slot][field]!r}, expected {expected!r}")

            expected_suffixes = {
                "color": "_color.tga",
                "normal": "_normal.tga",
                "gloss": "_extra.tga",
                "ao": "_extra.tga",
                "height": "_height.tga",
                "opacity": "_opacity.tga",
            }
            for slot, suffix in expected_suffixes.items():
                value = slots[slot]["filename"]
                if not value or not Path(value).name.lower().endswith(suffix):
                    errors.append(f"{label}: {slot} does not reference {suffix}")
                elif not resolve_ref(spm, value).is_file():
                    errors.append(f"{label}: missing {slot} file {resolve_ref(spm, value)}")
            if slots["gloss"]["filename"].lower() != slots["ao"]["filename"].lower():
                errors.append(f"{label}: Gloss and AO do not share extra")

            color_path = resolve_ref(spm, slots["color"]["filename"])
            if not color_path.name.lower().startswith("t_"):
                errors.append(f"{label}: Color is not a managed T_ output")
            base = color_path.name[:-len("_color.tga")]
            texture_sets[(str(color_path.parent).lower(), base.lower())] = (color_path.parent, base)

            sub = slots["subsurfacecolor"]
            if sub["enabled"] == "true":
                subsurface_enabled += 1
                if slots["subsurfaceamount"]["color_x"] != "1":
                    errors.append(f"{label}: enabled SubsurfaceAmount is not 1")
                if not sub["filename"].lower().endswith("_subsurface.tga"):
                    errors.append(f"{label}: enabled Subsurface has no managed output")
                elif not resolve_ref(spm, sub["filename"]).is_file():
                    errors.append(f"{label}: enabled Subsurface file is missing")
            else:
                subsurface_disabled += 1
                if any(sub[field] != "0" for field in ("color_x", "color_y", "color_z")):
                    errors.append(f"{label}: disabled SubsurfaceColor is not black")
                if slots["subsurfaceamount"]["color_x"] != "0":
                    errors.append(f"{label}: disabled SubsurfaceAmount is not 0")

    incomplete_sets = []
    subsurface_files = 0
    for folder, base in texture_sets.values():
        missing = [
            role for role in ("color", "normal", "extra", "height", "opacity")
            if not (folder / f"{base}_{role}.tga").is_file()
        ]
        if missing:
            incomplete_sets.append({"base": base, "folder": str(folder), "missing": missing})
        if (folder / f"{base}_subsurface.tga").is_file():
            subsurface_files += 1
    errors.extend(
        f"{row['base']}: missing set files {row['missing']}" for row in incomplete_sets)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ok": not errors,
        "sk_spms": len(spms),
        "materials": material_count,
        "texture_sets": len(texture_sets),
        "subsurface_files": subsurface_files,
        "subsurface_enabled": subsurface_enabled,
        "subsurface_disabled": subsurface_disabled,
        "preserved_cluster_materials": preserved_cluster_count,
        "no_material_property_spms": no_material_property,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--expect-spms", type=int)
    parser.add_argument("--expect-sets", type=int)
    args = parser.parse_args()
    result = verify()
    if args.expect_spms is not None and result["sk_spms"] != args.expect_spms:
        result["errors"].append(
            f"expected {args.expect_spms} SK SPMs, found {result['sk_spms']}")
    if args.expect_sets is not None and result["texture_sets"] != args.expect_sets:
        result["errors"].append(
            f"expected {args.expect_sets} texture sets, found {result['texture_sets']}")
    result["ok"] = not result["errors"]
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
