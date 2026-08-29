"""Transactionally remove one proven-unwanted SpeedTree generator.

This intentionally edits the XML text in place instead of reserializing the
whole SPM document.  SpeedTree SPM files contain large embedded payloads and a
full ElementTree round-trip would create an unnecessarily broad binary diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from spm_document import read_spm, write_spm


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blocks(text: str, tag: str):
    pattern = re.compile(
        rf"(?ms)^[ \t]*<{tag}\b[^>]*>.*?</{tag}>\r?\n?"
    )
    return list(pattern.finditer(text))


def _contains_field(block: str, field: str, value: str) -> bool:
    return re.search(
        rf"<{field}>[ \t\r\n]*{re.escape(value)}[ \t\r\n]*</{field}>",
        block,
    ) is not None


def remove_generator_text(text: str, guid: str):
    removals = []
    counts = {"generators": 0, "links": 0, "nodes": 0}
    for tag, fields, key in (
        ("Generator", ("GUID",), "generators"),
        ("Link", ("SourceGUID", "TargetGUID"), "links"),
        ("Node", ("GeneratorGUID",), "nodes"),
    ):
        for match in _blocks(text, tag):
            block = match.group(0)
            if any(_contains_field(block, field, guid) for field in fields):
                removals.append((match.start(), match.end()))
                counts[key] += 1

    if counts["generators"] != 1:
        raise RuntimeError(
            f"Expected exactly one Generator for {guid}; found "
            f"{counts['generators']}"
        )
    if counts["links"] < 1:
        raise RuntimeError(f"Generator {guid} has no removable Link")

    for start, end in sorted(removals, reverse=True):
        text = text[:start] + text[end:]

    if guid in text:
        raise RuntimeError(
            f"Generator GUID {guid} still occurs after structural removal"
        )
    return text, counts


def _generator_summary(text: str, guid: str):
    matches = [
        match.group(0)
        for match in _blocks(text, "Generator")
        if _contains_field(match.group(0), "GUID", guid)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one Generator for {guid}; found {len(matches)}"
        )
    block = matches[0]
    type_match = re.search(r"<Generator\b[^>]*\bType=\"([^\"]+)\"", block)
    name_match = re.search(r"<Name>(.*?)</Name>", block, re.S)
    material_ids = sorted(
        {
            int(value)
            for value in re.findall(
                r"<Name>[^<]*Material</Name>\s*<Value>(-?\d+)</Value>",
                block,
            )
        }
    )
    return {
        "type": type_match.group(1) if type_match else "",
        "name": (name_match.group(1).strip() if name_match else ""),
        "material_ids": material_ids,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--spm", required=True, type=Path)
    parser.add_argument("--generator-guid", required=True)
    parser.add_argument("--expected-type")
    parser.add_argument("--expected-name")
    parser.add_argument("--output-spm", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    source = args.spm.resolve()
    before_text = read_spm(source)
    summary = _generator_summary(before_text, args.generator_guid)
    if args.expected_type and summary["type"] != args.expected_type:
        raise RuntimeError(
            f"Generator type changed: {summary['type']!r} != "
            f"{args.expected_type!r}"
        )
    if args.expected_name and summary["name"] != args.expected_name:
        raise RuntimeError(
            f"Generator name changed: {summary['name']!r} != "
            f"{args.expected_name!r}"
        )

    after_text, counts = remove_generator_text(before_text, args.generator_guid)
    result = {
        "status": "dry_run",
        "source": str(source),
        "source_sha256": _sha256(source),
        "generator_guid": args.generator_guid,
        "generator": summary,
        "removed": counts,
    }

    if args.output_spm:
        output = args.output_spm.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
        write_spm(output, after_text)
        result.update(
            status="candidate_written",
            output=str(output),
            output_sha256=_sha256(output),
        )
    elif args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = source.parent / "_codex_backups" / stamp
        backup_dir.mkdir(parents=True, exist_ok=False)
        backup = backup_dir / source.name
        shutil.copy2(source, backup)
        if _sha256(backup) != result["source_sha256"]:
            raise RuntimeError("SPM backup hash mismatch; refusing source edit")
        write_spm(source, after_text)
        receipt = backup_dir / f"{source.stem}.generator_removal.json"
        result.update(
            status="applied",
            backup=str(backup),
            backup_sha256=_sha256(backup),
            output=str(source),
            output_sha256=_sha256(source),
            receipt=str(receipt),
        )
        receipt.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif not args.output_spm:
        result["note"] = "Pass --output-spm for a candidate or --apply."

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
