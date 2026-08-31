"""Repair authored shade-pruning thresholds for exact Assembly providers.

The normal SpeedTree Collision/Prune export is authoritative for the final
tree mesh.  A target SPM can nevertheless carry an accidentally aggressive
``Shade Pruning:Threshold`` on Frond generators that are populated by an
Assembly reference provider.  When that happens SpeedTree removes the
provider geometry before the native receipt is serialized, so the final
Assembly cannot contain those authored placements.

This tool changes the target SPM itself.  It resolves the exact active
Generator GUIDs from a sealed fleet report, patches only those Frond
generators, creates a hash-verified backup, and atomically promotes the new
SPM.  It does not disable Collision/Prune and it does not synthesize placement
or weight data.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

from speedtree_pipeline_contract import canonical_generator_guid


GENERATOR_BLOCK_RE = re.compile(
    r"<Generator\b(?P<attributes>[^>]*)>(?P<body>.*?)</Generator>",
    re.DOTALL,
)
GUID_RE = re.compile(r"<GUID>\s*(?P<value>[^<]+?)\s*</GUID>", re.DOTALL)
THRESHOLD_RE = re.compile(
    r"(?P<prefix><(?:Spline)?Property>\s*"
    r"<Name>\s*Shade Pruning:Threshold\s*</Name>\s*"
    r"<Value>)(?P<value>[^<]*)(?P<suffix></Value>)",
    re.DOTALL,
)
TYPE_RE = re.compile(r"\bType\s*=\s*(['\"])(?P<value>.*?)\1", re.DOTALL)


class ProviderPruningRepairError(RuntimeError):
    """The exact provider repair could not be proven or applied."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read_stable_spm(path: Path) -> tuple[bytes, str, bool, str]:
    candidate = Path(path).resolve()
    if not candidate.is_file() or candidate.suffix.casefold() != ".spm":
        raise ProviderPruningRepairError(f"Target SPM is missing: {candidate}")
    payload = None
    for _attempt in range(2):
        before = candidate.stat()
        current = candidate.read_bytes()
        after = candidate.stat()
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            payload = current
            break
    if payload is None:
        raise ProviderPruningRepairError(
            f"Target SPM changed while it was being read: {candidate}"
        )
    compressed = payload.startswith(b"\x1f\x8b")
    decoded = gzip.decompress(payload) if compressed else payload
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderPruningRepairError(
            f"Target SPM is not UTF-8 XML: {candidate}"
        ) from exc
    return payload, text, compressed, _sha256_bytes(payload)


def _canonical_path(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def _target_deliveries(report: dict, target_spm: Path):
    target_key = _canonical_path(target_spm)
    for item in report.get("items") or ():
        assembly = item.get("cluster_assembly") or {}
        for dependency in assembly.get("dependencies") or ():
            normalized = dependency.get("normalized_variants") or {}
            for delivery in normalized.get("target_deliveries") or ():
                delivery_path = delivery.get("spm") or delivery.get("target_spm")
                if not delivery_path:
                    continue
                if _canonical_path(delivery_path) == target_key:
                    yield dependency, delivery


def active_provider_guids(
    report: dict,
    target_spm: Path,
    provider_names: set[str],
) -> tuple[set[str], dict[str, list[str]]]:
    requested = {str(value).strip() for value in provider_names if str(value).strip()}
    if not requested:
        raise ProviderPruningRepairError("At least one provider name is required")
    found: dict[str, list[str]] = {name: [] for name in requested}
    for dependency, delivery in _target_deliveries(report, target_spm):
        name = str(dependency.get("name") or "").strip()
        if name not in requested:
            continue
        for binding in delivery.get("live_generator_bindings") or ():
            guid = canonical_generator_guid(binding.get("generator_guid"))
            if guid:
                found[name].append(guid)
    missing = sorted(name for name, values in found.items() if not values)
    if missing:
        raise ProviderPruningRepairError(
            "Fleet report has no active Generator GUIDs for provider(s): "
            + ", ".join(missing)
        )
    normalized = {
        name: sorted(set(values), key=str.casefold) for name, values in found.items()
    }
    return set().union(*normalized.values()), normalized


def _replace_generator_thresholds(
    text: str,
    wanted_guids: set[str],
    desired_threshold: str,
) -> tuple[str, list[dict]]:
    wanted = {canonical_generator_guid(value) for value in wanted_guids}
    seen: set[str] = set()
    changes: list[dict] = []

    def replace_block(match: re.Match) -> str:
        block = match.group(0)
        guid_match = GUID_RE.search(match.group("body"))
        if guid_match is None:
            return block
        guid = canonical_generator_guid(guid_match.group("value"))
        if guid not in wanted:
            return block
        seen.add(guid)
        type_match = TYPE_RE.search(match.group("attributes"))
        generator_type = type_match.group("value").strip() if type_match else ""
        if generator_type.casefold() != "frond":
            raise ProviderPruningRepairError(
                f"Assembly provider Generator is not Frond: {guid} ({generator_type})"
            )
        matches = list(THRESHOLD_RE.finditer(block))
        if len(matches) != 1:
            raise ProviderPruningRepairError(
                "Expected exactly one Shade Pruning:Threshold property for "
                f"Generator {guid}; found {len(matches)}"
            )
        property_match = matches[0]
        previous = property_match.group("value").strip()
        if previous == desired_threshold:
            return block
        changes.append(
            {
                "generator_guid": guid,
                "generator_type": generator_type,
                "before": previous,
                "after": desired_threshold,
            }
        )
        return (
            block[: property_match.start("value")]
            + desired_threshold
            + block[property_match.end("value") :]
        )

    patched = GENERATOR_BLOCK_RE.sub(replace_block, text)
    missing = sorted(wanted - seen, key=str.casefold)
    if missing:
        raise ProviderPruningRepairError(
            "Target SPM is missing active provider Generator GUID(s): "
            + ", ".join(missing)
        )
    return patched, sorted(changes, key=lambda row: row["generator_guid"].casefold())


def build_repair_plan(
    spm_path: str | Path,
    fleet_report_path: str | Path,
    provider_names: set[str],
    *,
    desired_threshold: str = "0.20000000298023224",
) -> dict:
    spm = Path(spm_path).resolve()
    report_path = Path(fleet_report_path).resolve()
    if not report_path.is_file():
        raise ProviderPruningRepairError(f"Fleet report is missing: {report_path}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderPruningRepairError(
            f"Fleet report is unreadable: {report_path}: {exc}"
        ) from exc
    payload, text, compressed, input_sha256 = _read_stable_spm(spm)
    wanted, provider_guids = active_provider_guids(
        report,
        spm,
        provider_names,
    )
    patched, changes = _replace_generator_thresholds(
        text,
        wanted,
        str(desired_threshold).strip(),
    )
    encoded = patched.encode("utf-8")
    output_payload = gzip.compress(encoded, compresslevel=9, mtime=0) if compressed else encoded
    return {
        "schema_version": 1,
        "status": "ready" if changes else "already_repaired",
        "target_spm": str(spm),
        "fleet_report": str(report_path),
        "providers": provider_guids,
        "desired_threshold": str(desired_threshold).strip(),
        "input_sha256": input_sha256,
        "output_sha256": _sha256_bytes(output_payload),
        "compressed": compressed,
        "change_count": len(changes),
        "changes": changes,
        "_input_payload": payload,
        "_output_payload": output_payload,
    }


def _public_plan(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def apply_repair_plan(plan: dict, *, backup_root: str | Path | None = None) -> dict:
    if plan.get("status") == "already_repaired":
        return {**_public_plan(plan), "applied": False, "backup": None}
    if plan.get("status") != "ready" or not plan.get("_output_payload"):
        raise ProviderPruningRepairError("Provider pruning repair plan is not ready")
    spm = Path(plan["target_spm"]).resolve()
    current_sha256 = _sha256_file(spm)
    if current_sha256 != plan["input_sha256"]:
        raise ProviderPruningRepairError(
            "Target SPM changed after the repair plan was built: "
            f"{current_sha256} != {plan['input_sha256']}"
        )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    root = (
        Path(backup_root).resolve()
        if backup_root is not None
        else spm.parent / "_spm_backups"
    )
    backup_dir = root / (
        f"assembly_provider_pruning_{stamp}_{plan['input_sha256'][:8]}"
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup = backup_dir / spm.name
    shutil.copy2(spm, backup)
    if _sha256_file(backup) != plan["input_sha256"]:
        raise ProviderPruningRepairError(
            f"SPM backup hash verification failed: {backup}"
        )
    temporary = spm.with_name(f".{spm.name}.{uuid.uuid4().hex}.tmp")
    promoted = False
    try:
        temporary.write_bytes(plan["_output_payload"])
        if _sha256_file(temporary) != plan["output_sha256"]:
            raise ProviderPruningRepairError(
                f"Temporary SPM hash verification failed: {temporary}"
            )
        os.replace(temporary, spm)
        promoted = True
        if _sha256_file(spm) != plan["output_sha256"]:
            raise ProviderPruningRepairError(
                f"Promoted SPM hash verification failed: {spm}"
            )
    except Exception:
        if promoted:
            os.replace(backup, spm)
            if _sha256_file(spm) != plan["input_sha256"]:
                raise ProviderPruningRepairError(
                    f"SPM rollback hash verification failed: {spm}"
                )
            shutil.copy2(spm, backup)
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        **_public_plan(plan),
        "status": "repaired",
        "applied": True,
        "backup": str(backup),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spm", required=True)
    parser.add_argument("--fleet-report", required=True)
    parser.add_argument("--provider", action="append", required=True)
    parser.add_argument(
        "--threshold",
        default="0.20000000298023224",
        help="Authored Shade Pruning threshold written into exact Frond generators",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_repair_plan(
            args.spm,
            args.fleet_report,
            set(args.provider),
            desired_threshold=args.threshold,
        )
        result = apply_repair_plan(plan) if args.apply else _public_plan(plan)
    except ProviderPruningRepairError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.report:
        report_path = Path(args.report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        os.replace(temporary, report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
