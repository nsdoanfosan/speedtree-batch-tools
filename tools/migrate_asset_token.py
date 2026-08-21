"""Transactional filename/content migration for one asset-tree spelling token."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sk_batch.spm_document import read_spm, write_spm  # noqa: E402
from process_lifecycle import owned_run  # noqa: E402


TEXT_SUFFIXES = {
    ".json",
    ".xml",
    ".stmat",
    ".ini",
    ".txt",
    ".md",
    ".sbs",
}
DEFAULT_EXCLUDED_PARTS = {
    "reports",
    "_spm_backups",
    "texture_normalize_backups",
    ".sk_batch_isolated_bark",
    ".speedtree_export_cache",
    ".atlas_leaf_speedtree_scopes",
    ".autosave",
}
DEFAULT_EXCLUDED_NAME_MARKERS = {
    ".pcgtex_backup_",
    ".skbatch_backup_",
    ".codex_backup_",
}


def _replace(value, old, new):
    return re.sub(re.escape(old), new, str(value), flags=re.IGNORECASE)


def _hash(path):
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidates(root, old, excluded_parts):
    old_key = old.casefold()
    rows = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.casefold() in excluded_parts for part in relative.parts):
            continue
        if any(
            marker in path.name.casefold()
            for marker in DEFAULT_EXCLUDED_NAME_MARKERS
        ):
            continue
        suffix = path.suffix.casefold()
        if suffix != ".blend" and suffix.startswith(".blend"):
            # Blender's numbered save versions are recovery data, not active
            # contracts. Preserve them byte-for-byte.
            continue
        matches = old_key in path.name.casefold()
        if not matches and suffix in TEXT_SUFFIXES:
            try:
                matches = old_key in path.read_text(encoding="utf-8").casefold()
            except UnicodeError:
                matches = False
        elif not matches and suffix == ".spm":
            matches = old_key in read_spm(path).casefold()
        elif not matches and suffix in {".blend", ".blend1"}:
            matches = old_key.encode("ascii") in path.read_bytes().lower()
        if not matches:
            continue
        rows.append(path)
    return sorted(rows, key=lambda path: str(path).casefold())


def _replace_text_file(path, old, new):
    text = path.read_text(encoding="utf-8")
    replaced = _replace(text, old, new)
    if replaced != text:
        path.write_text(replaced, encoding="utf-8")
        return True
    return False


def _replace_spm(path, old, new):
    text = read_spm(path)
    replaced = _replace(text, old, new)
    if replaced != text:
        write_spm(path, replaced)
        return True
    return False


def _replace_blend(path, old, new, blender, report_path):
    helper = REPO_ROOT / "tools" / "blender_replace_asset_token.py"
    command = [
        str(blender),
        "--factory-startup",
        "--background",
        str(path),
        "--python",
        str(helper),
        "--",
        "--old",
        old,
        "--new",
        new,
        "--apply",
        "--report",
        str(report_path),
    ]
    completed = owned_run(
        command,
        source="tools.migrate_asset_token.blender",
        run_factory=subprocess.run,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise RuntimeError(
            f"Blender token migration failed for {path}: {completed.stdout[-4000:]}"
        )
    return json.loads(report_path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--include-atlas-scope-contracts",
        action="store_true",
        help=(
            "also migrate active .atlas_leaf_speedtree_scopes contracts; "
            "history/backup folders remain excluded"
        ),
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"asset root does not exist: {root}")
    excluded_parts = set(DEFAULT_EXCLUDED_PARTS)
    if args.include_atlas_scope_contracts:
        excluded_parts.discard(".atlas_leaf_speedtree_scopes")
    sources = _candidates(root, args.old, excluded_parts)
    rows = [
        (source, source.with_name(_replace(source.name, args.old, args.new)))
        for source in sources
    ]
    conflicts = [
        str(target)
        for source, target in rows
        if target != source and target.exists()
    ]
    if conflicts:
        raise SystemExit("rename target already exists: " + "; ".join(conflicts))

    payload = {
        "root": str(root),
        "old": args.old,
        "new": args.new,
        "applied": False,
        "candidate_count": len(rows),
        "conflicts": conflicts,
        "include_atlas_scope_contracts": bool(
            args.include_atlas_scope_contracts
        ),
        "files": [
            {
                "source": str(source),
                "target": str(target),
                "size": source.stat().st_size,
                "hash_before": _hash(source),
            }
            for source, target in rows
        ],
        "content_changes": [],
        "blend_reports": [],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    if not args.apply:
        args.report.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return

    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_root = args.report.parent / f"asset_token_backup_{stamp}"
    payload["backup_root"] = str(backup_root)
    for source, _target in rows:
        backup = backup_root / source.relative_to(root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)

    renamed_targets = []
    try:
        for index, (source, _target) in enumerate(rows):
            suffix = source.suffix.casefold()
            changed = False
            if suffix in {".blend", ".blend1"}:
                blend_report = args.report.parent / (
                    f"{source.stem}_blend_token_{index:03d}.json"
                )
                result = _replace_blend(
                    source,
                    args.old,
                    args.new,
                    args.blender,
                    blend_report,
                )
                payload["blend_reports"].append(result)
                changed = bool(result.get("change_count"))
            elif suffix == ".spm":
                changed = _replace_spm(source, args.old, args.new)
            elif suffix in TEXT_SUFFIXES:
                changed = _replace_text_file(source, args.old, args.new)
            if changed:
                payload["content_changes"].append(str(source))

        for source, target in rows:
            if target == source:
                continue
            os.replace(source, target)
            renamed_targets.append(target)

        payload["applied"] = True
        payload["files_after"] = [
            {
                "path": str(target),
                "hash_after": _hash(target),
            }
            for _source, target in rows
        ]
    except Exception:
        for target in reversed(renamed_targets):
            if target.exists():
                target.unlink()
        for source, _target in rows:
            backup = backup_root / source.relative_to(root)
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, source)
        raise
    finally:
        args.report.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
