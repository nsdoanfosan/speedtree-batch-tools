"""Repair one exact SpeedTree material texture slot without reserializing XML."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sk_batch.spm_document import read_spm, write_spm  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spm", required=True)
    parser.add_argument("--material", action="append", required=True)
    parser.add_argument(
        "--map",
        dest="map_names",
        action="append",
        required=True,
    )
    parser.add_argument("--from-ref", action="append", required=True)
    parser.add_argument("--to-ref", action="append", required=True)
    return parser.parse_args()


def replace_exact_texture_slot(
    text,
    *,
    material_name,
    map_name,
    from_ref,
    to_ref,
):
    material_pattern = re.compile(
        (
            r'(<Material_v8\b(?=[^>]*\bName="'
            + re.escape(material_name)
            + r'")[^>]*>.*?</Material_v8>)'
        ),
        re.DOTALL,
    )
    materials = list(material_pattern.finditer(text))
    if len(materials) != 1:
        raise RuntimeError(
            f"Expected one material {material_name!r}, found {len(materials)}."
        )

    material_block = materials[0].group(1)
    map_pattern = re.compile(
        (
            r'(<Map\b(?=[^>]*\bName="'
            + re.escape(map_name)
            + r'")[^>]*>.*?<TexFilename>)'
            + re.escape(from_ref)
            + r'(</TexFilename>.*?</Map>)'
        ),
        re.DOTALL,
    )
    maps = list(map_pattern.finditer(material_block))
    if len(maps) != 1:
        raise RuntimeError(
            f"Expected one {map_name!r} slot with {from_ref!r}, "
            f"found {len(maps)}."
        )
    patched_block = map_pattern.sub(
        lambda match: match.group(1) + to_ref + match.group(2),
        material_block,
        count=1,
    )
    start, end = materials[0].span(1)
    return text[:start] + patched_block + text[end:]


def main():
    args = parse_args()
    spm = Path(args.spm).expanduser().resolve()
    if not spm.is_file():
        raise FileNotFoundError(spm)
    if not (
        len(args.map_names)
        == len(args.from_ref)
        == len(args.to_ref)
    ):
        raise RuntimeError(
            "--map, --from-ref, and --to-ref counts must match."
        )
    replacements = list(zip(
        args.map_names,
        args.from_ref,
        args.to_ref,
    ))
    targets = []
    for _map_name, _from_ref, to_ref in replacements:
        target = (spm.parent / to_ref).resolve()
        if not target.is_file():
            raise FileNotFoundError(
                f"Replacement texture does not exist: {target}"
            )
        targets.append(target)

    original = read_spm(spm)
    patched = original
    repairs = []
    for material_name in args.material:
        for (map_name, from_ref, to_ref), target in zip(
            replacements,
            targets,
        ):
            patched = replace_exact_texture_slot(
                patched,
                material_name=material_name,
                map_name=map_name,
                from_ref=from_ref,
                to_ref=to_ref,
            )
            repairs.append({
                "material": material_name,
                "map": map_name,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "target_texture": str(target),
            })
    if patched == original:
        raise RuntimeError("Texture-slot repair produced no change.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = spm.with_name(
        f"{spm.stem}.texture_slot_backup_{timestamp}{spm.suffix}"
    )
    shutil.copy2(spm, backup)
    try:
        write_spm(spm, patched)
        verified = read_spm(spm)
        if verified != patched:
            raise RuntimeError("SPM verification differed after atomic write.")
    except Exception:
        shutil.copy2(backup, spm)
        raise

    print(json.dumps(
        {
            "status": "repaired",
            "spm": str(spm),
            "backup": str(backup),
            "repairs": repairs,
        },
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
