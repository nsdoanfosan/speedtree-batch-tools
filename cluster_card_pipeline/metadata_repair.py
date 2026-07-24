"""Explicit, evidence-backed repair for stale SpeedTree texture-size metadata."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from .contract import ContractError, _fingerprint, _read_spm_bytes


def _tga_size(path: Path) -> list[int]:
    header = path.read_bytes()[:18]
    if len(header) != 18:
        raise ContractError(f"Texture has no valid TGA header: {path}")
    size = [
        int.from_bytes(header[12:14], "little"),
        int.from_bytes(header[14:16], "little"),
    ]
    if min(size) <= 0:
        raise ContractError(f"Texture has invalid TGA dimensions {size}: {path}")
    return size


def _required_int(node: ET.Element, name: str, label: str) -> int:
    value = node.findtext(name)
    try:
        return int(value or "0")
    except ValueError as exc:
        raise ContractError(f"{label} has invalid {name}: {value!r}") from exc


def inspect_texture_size_repairs(spm_path, material_name: str) -> dict:
    spm = Path(spm_path).expanduser().resolve()
    before = _fingerprint(spm)
    try:
        root = ET.fromstring(_read_spm_bytes(spm))
    except ET.ParseError as exc:
        raise ContractError(f"Unable to parse SPM XML: {spm}: {exc}") from exc
    materials = [
        node
        for node in root.findall(".//Material_v8")
        if str(node.get("Name") or "") == material_name
    ]
    if len(materials) != 1:
        raise ContractError(
            f"Material '{material_name}' must resolve exactly once; found {len(materials)}"
        )
    material = materials[0]
    expected = [
        _required_int(material, "Width", material_name),
        _required_int(material, "Height", material_name),
    ]
    if min(expected) <= 0:
        raise ContractError(f"Material '{material_name}' has invalid atlas size {expected}")

    proposals = []
    verified_maps = []
    for map_node in material.findall("Map"):
        map_name = str(map_node.get("Name") or "")
        stored = str(map_node.findtext("TexFilename") or "").strip()
        if not stored:
            continue
        texture = (spm.parent / stored).resolve()
        if not texture.is_file():
            raise ContractError(f"Material '{material_name}' map '{map_name}' is missing: {texture}")
        if texture.suffix.casefold() != ".tga":
            raise ContractError(
                f"Texture-size repair only accepts authoritative TGA maps: {texture}"
            )
        declared = [
            _required_int(map_node, "TexSizeX", f"{material_name}/{map_name}"),
            _required_int(map_node, "TexSizeY", f"{material_name}/{map_name}"),
        ]
        actual = _tga_size(texture)
        if actual != expected:
            raise ContractError(
                f"Material '{material_name}' map '{map_name}' header {actual} "
                f"does not match material size {expected}"
            )
        row = {
            "map": map_name,
            "texture": _fingerprint(texture),
            "declared_before": declared,
            "actual_tga_header": actual,
            "expected_material_size": expected,
        }
        verified_maps.append(row)
        if declared != expected:
            proposals.append(row)

    if not verified_maps:
        raise ContractError(f"Material '{material_name}' has no texture maps to verify")
    current = _fingerprint(spm)
    if current["sha256"] != before["sha256"]:
        raise ContractError("SPM changed while texture metadata was being inspected")
    return {
        "schema_version": 1,
        "kind": "speedtree_texture_size_metadata_repair",
        "spm_before": before,
        "material": {
            "name": material_name,
            "id": int(material.get("ID") or -1),
            "expected_size": expected,
        },
        "verified_maps": verified_maps,
        "repairs": proposals,
        "status": "repair_required" if proposals else "already_canonical",
    }


def repair_texture_size_metadata(spm_path, material_name: str, *, apply: bool = False) -> dict:
    report = inspect_texture_size_repairs(spm_path, material_name)
    if not apply or not report["repairs"]:
        report["applied"] = False
        report["spm_after"] = report["spm_before"]
        return report

    spm = Path(spm_path).expanduser().resolve()
    source_hash = report["spm_before"]["sha256"]
    if _fingerprint(spm)["sha256"] != source_hash:
        raise ContractError("SPM changed before texture metadata repair could be applied")
    root = ET.fromstring(_read_spm_bytes(spm))
    material = next(
        node
        for node in root.findall(".//Material_v8")
        if str(node.get("Name") or "") == material_name
    )
    repair_by_name = {row["map"]: row for row in report["repairs"]}
    for map_node in material.findall("Map"):
        row = repair_by_name.get(str(map_node.get("Name") or ""))
        if row is None:
            continue
        for field, value in zip(("TexSizeX", "TexSizeY"), row["expected_material_size"]):
            child = map_node.find(field)
            if child is None:
                raise ContractError(f"Map '{row['map']}' is missing required field {field}")
            child.text = str(value)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = spm.parent / "_spm_backups" / f"texture_metadata_repair_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup = backup_dir / spm.name
    shutil.copy2(spm, backup)
    if _fingerprint(backup)["sha256"] != source_hash:
        raise ContractError("Texture metadata repair backup hash does not match the source SPM")

    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=spm.stem + ".metadata_repair_",
        suffix=spm.suffix,
        dir=spm.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        handle.write(gzip.compress(payload, mtime=0))
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, spm)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()

    verified = inspect_texture_size_repairs(spm, material_name)
    if verified["repairs"]:
        raise ContractError("Texture metadata repair did not produce canonical map sizes")
    report["applied"] = True
    report["backup"] = _fingerprint(backup)
    report["spm_after"] = verified["spm_before"]
    report["status"] = "complete"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spm", required=True)
    parser.add_argument("--material", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    payload = repair_texture_size_metadata(
        args.spm,
        args.material,
        apply=args.apply,
    )
    report_path = Path(args.report).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(report_path))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(str(exc), file=os.sys.stderr)
        raise SystemExit(2)
