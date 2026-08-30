"""Fail-closed finalization for a completed forced affected-target refresh.

This module is deliberately downstream of production.  It never exports,
imports, retries, skips, or reuses an asset.  It only audits immutable evidence
from one or more fleet attempts and, after all expected targets have one sealed
success, can publish deployment receipts through a crash-resumable journal.

The command line defaults to audit-only.  Receipt publication requires the
explicit ``--write-receipts`` switch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from push_unreal_recovery import (
    PUSH_CONTRACT_KEYS,
    PushUnrealRecoveryError,
    stable_fingerprint,
    validate_item_artifacts,
)


INVENTORY_COUNT = 80
RECEIPT_SCHEMA_VERSION = 2
JOURNAL_SCHEMA_VERSION = 1
COMMIT_LEDGER_SCHEMA_VERSION = 1
SUCCESS = "valid_success"
AMBIGUOUS_FAILURE = "ambiguous_failure"
INVALID = "invalid"
FRESH_PUSH_CONTRACT_KEYS = tuple(PUSH_CONTRACT_KEYS) + (
    "export_contracts",
)


class FinalizationError(RuntimeError):
    """Evidence cannot safely authorize deployment receipts."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_bytes(value: Any) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_path(value: Any) -> str:
    """Return one Windows-safe identity key for an existing or planned path."""
    return os.path.normcase(
        str(Path(str(value)).expanduser().resolve(strict=False))
    ).casefold()


@dataclass(frozen=True)
class JsonSnapshot:
    path: Path
    payload: dict[str, Any]
    sha256: str
    size: int
    mtime_ns: int


def read_stable_json(
    path: Path | str,
    *,
    attempts: int = 3,
    retry_delay: float = 0.02,
) -> JsonSnapshot:
    """Read JSON only when its size and mtime stay fixed across the read."""
    candidate = Path(path).resolve()
    last_error: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            before = candidate.stat()
            raw = candidate.read_bytes()
            after = candidate.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or len(raw) != after.st_size
            ):
                raise FinalizationError(
                    f"source changed during read: {candidate}"
                )
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise FinalizationError(f"JSON root is not an object: {candidate}")
            return JsonSnapshot(
                path=candidate,
                payload=payload,
                sha256=_sha256_bytes(raw),
                size=after.st_size,
                mtime_ns=after.st_mtime_ns,
            )
        except (OSError, UnicodeDecodeError, ValueError, FinalizationError) as exc:
            last_error = exc
            if attempt + 1 < max(1, int(attempts)):
                time.sleep(max(0.0, float(retry_delay)))
    raise FinalizationError(f"stable JSON read failed: {candidate}: {last_error}")


def verify_snapshot_unchanged(snapshot: JsonSnapshot) -> None:
    try:
        stat_result = snapshot.path.stat()
    except OSError as exc:
        raise FinalizationError(
            f"evidence disappeared after audit: {snapshot.path}"
        ) from exc
    if stat_result.st_size != snapshot.size:
        raise FinalizationError(f"evidence size changed after audit: {snapshot.path}")
    if _sha256_file(snapshot.path) != snapshot.sha256:
        raise FinalizationError(f"evidence content changed after audit: {snapshot.path}")


@dataclass(frozen=True)
class InventoryTarget:
    ordinal: int
    stem: str
    spm: Path
    canonical_spm: str
    manifest: Path
    native_receipt: Path
    deployment_receipt: Path
    source: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class OrderedInventory:
    path: Path
    targets: tuple[InventoryTarget, ...]
    vector_sha256: str
    snapshot: JsonSnapshot


def load_ordered_inventory(
    path: Path | str,
    *,
    expected_count: int = INVENTORY_COUNT,
) -> OrderedInventory:
    snapshot = read_stable_json(path)
    rows = snapshot.payload.get("selected")
    if not isinstance(rows, list):
        raise FinalizationError("inventory selected[] is missing")
    if len(rows) != int(expected_count):
        raise FinalizationError(
            f"inventory count is {len(rows)}, expected {expected_count}"
        )
    targets: list[InventoryTarget] = []
    seen_spms: set[str] = set()
    seen_receipts: set[str] = set()
    vector: list[dict[str, Any]] = []
    for ordinal, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise FinalizationError(f"inventory row {ordinal} is not an object")
        stem = str(row.get("stem") or "")
        spm_text = str(row.get("spm") or "")
        manifest_text = str(row.get("manifest") or "")
        if not stem or not spm_text or not manifest_text:
            raise FinalizationError(f"inventory row {ordinal} is incomplete")
        spm = Path(spm_text).expanduser().resolve(strict=False)
        manifest = Path(manifest_text).expanduser().resolve(strict=False)
        native = Path(
            row.get("receipt")
            or spm.parent / "fbx" / f"{stem}.speedtree_native_receipt.json"
        ).expanduser().resolve(strict=False)
        deployment = Path(
            row.get("deployment_receipt")
            or spm.parent / "fbx" / f"{stem}.unreal_deployment_receipt.json"
        ).expanduser().resolve(strict=False)
        spm_key = canonical_path(spm)
        receipt_key = canonical_path(deployment)
        if spm_key in seen_spms:
            raise FinalizationError(f"duplicate inventory SPM: {spm}")
        if receipt_key in seen_receipts:
            raise FinalizationError(
                f"duplicate deployment receipt path: {deployment}"
            )
        seen_spms.add(spm_key)
        seen_receipts.add(receipt_key)
        target = InventoryTarget(
            ordinal=ordinal,
            stem=stem,
            spm=spm,
            canonical_spm=spm_key,
            manifest=manifest,
            native_receipt=native,
            deployment_receipt=deployment,
            source=dict(row),
        )
        targets.append(target)
        # selected is intentionally omitted. Resume rewrites that diagnostic
        # boolean, while the original ordered vector remains authoritative.
        vector.append({
            "ordinal": ordinal,
            "stem": stem,
            "spm": str(spm),
            "manifest": str(manifest),
            "native_receipt": str(native),
            "deployment_receipt": str(deployment),
        })
    return OrderedInventory(
        path=snapshot.path,
        targets=tuple(targets),
        vector_sha256=hashlib.sha256(
            _canonical_json(vector).encode("utf-8")
        ).hexdigest(),
        snapshot=snapshot,
    )


@dataclass(frozen=True)
class FleetSource:
    run_id: str
    snapshot: JsonSnapshot
    results_by_spm: dict[str, dict[str, Any]]


def load_fleet_sources(
    log_dir: Path | str,
    run_ids: Iterable[str],
    *,
    allow_abandoned_run_ids: Iterable[str] = (),
) -> dict[str, FleetSource]:
    root = Path(log_dir).resolve()
    allowed_abandoned = set(allow_abandoned_run_ids)
    sources: dict[str, FleetSource] = {}
    for run_id in run_ids:
        path = root / f"cluster_fleet_push_{run_id}.json"
        snapshot = read_stable_json(path)
        status = str(snapshot.payload.get("status") or "")
        if status not in {"ok", "failed", "running"}:
            raise FinalizationError(
                f"unsupported fleet source status for {run_id}: {status}"
            )
        if status == "running" and run_id not in allowed_abandoned:
            raise FinalizationError(f"ACTIVE_SOURCE_WRITER:{run_id}")
        by_spm: dict[str, dict[str, Any]] = {}
        for row in snapshot.payload.get("results") or []:
            if not isinstance(row, dict) or not row.get("spm"):
                raise FinalizationError(f"invalid fleet result in {path}")
            key = canonical_path(row["spm"])
            if key in by_spm:
                raise FinalizationError(
                    f"duplicate SPM in one fleet report: {row['spm']}"
                )
            by_spm[key] = row
        sources[run_id] = FleetSource(run_id, snapshot, by_spm)
    return sources


@dataclass(frozen=True)
class ExactBundle:
    run_id: str
    bundle_id: str
    manifest: Path
    checkpoint: Path
    batch: Path
    item_report: Path
    exact_report: Path
    assembly_report: Path
    fleet_report: Path


def discover_exact_bundles(
    log_dir: Path | str,
    run_ids: Iterable[str],
) -> list[ExactBundle]:
    root = Path(log_dir).resolve()
    bundles: list[ExactBundle] = []
    for run_id in run_ids:
        marker = f"_exact_push_fleet_{run_id}_"
        for manifest in sorted(root.glob(f"*{marker}*_manifest.json")):
            name = manifest.name
            if "_provider_" in name:
                continue
            base_name = name[: -len("_manifest.json")]
            if marker not in base_name:
                continue
            ordinal_suffix = base_name.split(marker, 1)[1]
            if len(ordinal_suffix) != 3 or not ordinal_suffix.isdigit():
                # A run id may be a strict prefix of a later tail run id.
                # Accept only this run's exact three-digit root ordinal.
                continue
            assembly_name = base_name.replace(
                "_exact_push_fleet_", "_fleet_assembly_", 1
            ) + ".json"
            bundles.append(ExactBundle(
                run_id=run_id,
                bundle_id=base_name,
                manifest=manifest.resolve(),
                checkpoint=(root / f"{base_name}_checkpoint.json").resolve(),
                batch=(root / f"{base_name}_batch.json").resolve(),
                item_report=(root / f"{base_name}_unreal.json").resolve(),
                exact_report=(root / f"{base_name}.json").resolve(),
                assembly_report=(root / assembly_name).resolve(),
                fleet_report=(root / f"cluster_fleet_push_{run_id}.json").resolve(),
            ))
    return bundles


def _parse_time(value: Any, label: str) -> datetime:
    text = str(value or "")
    if not text:
        raise FinalizationError(f"{label} timestamp is missing")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinalizationError(f"{label} timestamp is invalid: {text}") from exc


def _single_state(
    payload: dict[str, Any],
    target: InventoryTarget,
    label: str,
) -> tuple[str, dict[str, Any]]:
    states = payload.get("items")
    if not isinstance(states, dict) or len(states) != 1:
        raise FinalizationError(f"{label} must contain exactly one item")
    queue_id, state = next(iter(states.items()))
    if canonical_path(queue_id) != target.canonical_spm:
        raise FinalizationError(f"{label} queue id does not match inventory SPM")
    if not isinstance(state, dict):
        raise FinalizationError(f"{label} item state is invalid")
    return str(queue_id), state


def _validate_unreal_postcondition(
    item: dict[str, Any],
    state: dict[str, Any],
) -> None:
    if state.get("status") != "imported_ok":
        raise FinalizationError("Unreal item status is not imported_ok")
    materials = state.get("materials") or {}
    section_validation = materials.get("section_material_validation") or {}
    if section_validation.get("status") != "ok":
        raise FinalizationError("exact material section validation is not ok")
    if canonical_path(materials.get("mesh") or "") != canonical_path(
        item.get("mesh_path") or ""
    ):
        raise FinalizationError("material receipt mesh does not match item mesh")
    skeleton = state.get("skeleton") or {}
    skeleton_contract = skeleton.get("final_skeleton_contract") or {}
    try:
        bone_count = int(skeleton_contract.get("bone_count") or 0)
    except (TypeError, ValueError) as exc:
        raise FinalizationError("final Skeleton bone count is invalid") from exc
    skeleton_hash = str(skeleton_contract.get("hash") or "")
    saved = state.get("final_skeleton_saved") or {}
    if bone_count <= 0 or not skeleton_hash:
        raise FinalizationError("final Skeleton contract is incomplete")
    if str(saved.get("mesh") or "") != str(item.get("mesh_path") or ""):
        raise FinalizationError("saved final Skeleton mesh does not match item")
    if not saved.get("skeleton"):
        raise FinalizationError("saved final Skeleton asset is missing")
    assembly = state.get("cluster_assembly") or {}
    if item.get("cluster_assembly") is None:
        if assembly.get("status") != "skipped":
            raise FinalizationError("pass-through Assembly was not skipped")
        return
    build = assembly.get("build") or {}
    parts = list(build.get("parts") or [])
    wind = build.get("dynamic_wind") or {}
    provenance = build.get("provenance") or {}
    if assembly.get("status") != "ok" or build.get("status") != "ok":
        raise FinalizationError("Unreal Assembly build is not ok")
    if not parts or any(int(row.get("bindings") or 0) <= 0 for row in parts):
        raise FinalizationError("Unreal Assembly part/binding contract is incomplete")
    if int(build.get("binding_count") or 0) != sum(
        int(row.get("bindings") or 0) for row in parts
    ):
        raise FinalizationError("Unreal Assembly binding count is inconsistent")
    if int(build.get("final_skeleton_bones") or 0) != bone_count:
        raise FinalizationError("Assembly and final Skeleton bone counts differ")
    if str(wind.get("skeleton_hash") or "") != skeleton_hash:
        raise FinalizationError("Assembly wind Skeleton hash is inconsistent")
    if wind.get("success") is not True or provenance.get("success") is not True:
        raise FinalizationError("Assembly wind/provenance postcondition failed")


@dataclass(frozen=True)
class CandidateVerdict:
    target: InventoryTarget
    bundle: ExactBundle
    classification: str
    fingerprint: str
    started_at: datetime
    event_at: datetime
    verification_basis: str
    snapshots: tuple[JsonSnapshot, ...]
    errors: tuple[str, ...] = ()


def validate_exact_bundle(
    bundle: ExactBundle,
    target: InventoryTarget,
    *,
    fleet_result: dict[str, Any] | None = None,
    verify_artifacts: bool = True,
) -> CandidateVerdict:
    snapshots: list[JsonSnapshot] = []
    try:
        manifest_snapshot = read_stable_json(bundle.manifest)
        snapshots.append(manifest_snapshot)
        manifest = manifest_snapshot.payload
        if manifest.get("schema_version") != 1:
            raise FinalizationError("exact manifest schema is not 1")
        items = manifest.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise FinalizationError("exact manifest must contain exactly one item")
        item = items[0]
        if not isinstance(item, dict):
            raise FinalizationError("exact manifest item is invalid")
        if canonical_path(item.get("queue_id") or "") != target.canonical_spm:
            raise FinalizationError("manifest queue id does not match inventory SPM")
        if canonical_path(manifest.get("checkpoint_path") or "") != canonical_path(
            bundle.checkpoint
        ):
            raise FinalizationError("manifest checkpoint path differs from bundle")
        if canonical_path(manifest.get("report_path") or "") != canonical_path(
            bundle.batch
        ):
            raise FinalizationError("manifest batch-report path differs from bundle")
        if item.get("recovery") or item.get("verify_existing_assets") is True:
            raise FinalizationError("evidence is not a forced-fresh item")
        contract = {key: item.get(key) for key in FRESH_PUSH_CONTRACT_KEYS}
        expected_fingerprint = stable_fingerprint(contract)
        fingerprint = str(item.get("fingerprint") or "")
        if not fingerprint or fingerprint != expected_fingerprint:
            raise FinalizationError("manifest item fingerprint is invalid")
        if verify_artifacts:
            try:
                validate_item_artifacts(item)
            except PushUnrealRecoveryError as exc:
                raise FinalizationError(str(exc)) from exc

        if fleet_result is not None:
            if canonical_path(fleet_result.get("spm") or "") != target.canonical_spm:
                raise FinalizationError("fleet result SPM differs from inventory")
            if str(fleet_result.get("stem") or "") != target.stem:
                raise FinalizationError("fleet result stem differs from inventory")
            declared_report = fleet_result.get("report")
            if declared_report and canonical_path(declared_report) != canonical_path(
                bundle.exact_report
            ):
                raise FinalizationError("fleet result report differs from exact bundle")

        checkpoint_snapshot = read_stable_json(bundle.checkpoint)
        snapshots.append(checkpoint_snapshot)
        checkpoint = checkpoint_snapshot.payload
        if canonical_path(checkpoint.get("manifest") or "") != canonical_path(
            bundle.manifest
        ):
            raise FinalizationError("checkpoint manifest path differs from bundle")
        _, checkpoint_state = _single_state(checkpoint, target, "checkpoint")
        if checkpoint_state.get("fingerprint") != fingerprint:
            raise FinalizationError("checkpoint fingerprint differs from manifest")
        started_at = _parse_time(
            checkpoint_state.get("started_at"), "checkpoint started_at"
        )

        # A failed/ambiguous attempt is still structurally useful history.  It
        # may be sealed only by a strictly later fully validated success.
        checkpoint_status = str(checkpoint_state.get("status") or "")
        if checkpoint_status != "imported_ok" or checkpoint.get("complete") is not True:
            event_at = _parse_time(
                checkpoint_state.get("updated_at"), "checkpoint updated_at"
            )
            return CandidateVerdict(
                target=target,
                bundle=bundle,
                classification=AMBIGUOUS_FAILURE,
                fingerprint=fingerprint,
                started_at=started_at,
                event_at=event_at,
                verification_basis="incomplete_or_failed_rpc_attempt",
                snapshots=tuple(snapshots),
            )
        if checkpoint.get("current_item") not in (None, ""):
            raise FinalizationError("complete checkpoint still has current_item")

        batch_snapshot = read_stable_json(bundle.batch)
        item_snapshot = read_stable_json(bundle.item_report)
        snapshots.extend((batch_snapshot, item_snapshot))
        batch = batch_snapshot.payload
        item_report = item_snapshot.payload
        if canonical_path(batch.get("manifest") or "") != canonical_path(
            bundle.manifest
        ):
            raise FinalizationError("batch manifest path differs from bundle")
        if canonical_path(batch.get("checkpoint") or "") != canonical_path(
            bundle.checkpoint
        ):
            raise FinalizationError("batch checkpoint path differs from bundle")
        _, batch_state = _single_state(batch, target, "batch")
        if batch.get("status") != "complete":
            raise FinalizationError("batch status is not complete")
        counts = batch.get("counts") or {}
        if counts != {"imported_ok": 1}:
            raise FinalizationError("batch counts are not exactly imported_ok=1")
        for label, state in (
            ("checkpoint", checkpoint_state),
            ("batch", batch_state),
            ("item report", item_report),
        ):
            if state.get("status") != "imported_ok":
                raise FinalizationError(f"{label} is not imported_ok")
            if state.get("fingerprint") != fingerprint:
                raise FinalizationError(f"{label} fingerprint differs from manifest")
        if canonical_path(item_report.get("queue_id") or "") != target.canonical_spm:
            raise FinalizationError("item report queue id differs from inventory")
        if canonical_path(item_report.get("manifest") or "") != canonical_path(
            bundle.manifest
        ):
            raise FinalizationError("item report manifest path differs from bundle")
        if canonical_path(item_report.get("checkpoint") or "") != canonical_path(
            bundle.checkpoint
        ):
            raise FinalizationError("item report checkpoint path differs from bundle")
        if canonical_path(item_report.get("report") or "") != canonical_path(
            bundle.item_report
        ):
            raise FinalizationError("item report self path differs from bundle")
        agreement_keys = (
            "status",
            "fingerprint",
            "started_at",
            "updated_at",
            "completed_at",
            "materials",
            "skeleton",
            "final_skeleton_saved",
            "cluster_assembly",
        )
        for key in agreement_keys:
            expected_value = batch_state.get(key)
            if checkpoint_state.get(key) != expected_value:
                raise FinalizationError(
                    f"checkpoint and batch disagree on {key}"
                )
            if item_report.get(key) != expected_value:
                raise FinalizationError(
                    f"item report and batch disagree on {key}"
                )
        if item_report.get("asset_cache"):
            raise FinalizationError("item report used an existing-asset fast path")
        _validate_unreal_postcondition(item, batch_state)

        assembly_snapshot = read_stable_json(bundle.assembly_report)
        snapshots.append(assembly_snapshot)
        assembly_payload = assembly_snapshot.payload
        if assembly_payload.get("status") != "ok":
            raise FinalizationError("Assembly export report status is not ok")
        assembly_spm = assembly_payload.get("speedtree_spm") or assembly_payload.get(
            "spm"
        )
        if not assembly_spm or canonical_path(assembly_spm) != target.canonical_spm:
            raise FinalizationError("Assembly export report SPM differs from inventory")

        verification_basis = "derived_atomic_ingest_bundle_v1"
        if bundle.exact_report.is_file():
            report_snapshot = read_stable_json(bundle.exact_report)
            snapshots.append(report_snapshot)
            report = report_snapshot.payload
            if report.get("status") != "ok":
                raise FinalizationError("exact report status is not ok")
            if report.get("manifest_fingerprint") != fingerprint:
                raise FinalizationError("exact report fingerprint differs from manifest")
            if (report.get("unreal_result") or {}).get("status") != "imported_ok":
                raise FinalizationError("exact report Unreal result is not imported_ok")
            verification_basis = "exact_report_and_atomic_ingest_bundle_v1"

        if fleet_result is not None:
            if fleet_result.get("status") == "failed":
                raise FinalizationError(
                    "fleet declared failure despite terminal exact evidence"
                )
            if fleet_result.get("status") != "verified_in_unreal":
                raise FinalizationError("fleet result is not verified_in_unreal")

        event_at = _parse_time(
            batch_state.get("completed_at") or batch.get("completed_at"),
            "successful attempt completed_at",
        )
        return CandidateVerdict(
            target=target,
            bundle=bundle,
            classification=SUCCESS,
            fingerprint=fingerprint,
            started_at=started_at,
            event_at=event_at,
            verification_basis=verification_basis,
            snapshots=tuple(snapshots),
        )
    except (FinalizationError, OSError, ValueError, TypeError, KeyError) as exc:
        now = datetime.min
        return CandidateVerdict(
            target=target,
            bundle=bundle,
            classification=INVALID,
            fingerprint="",
            started_at=now,
            event_at=now,
            verification_basis="invalid",
            snapshots=tuple(snapshots),
            errors=(str(exc),),
        )


@dataclass(frozen=True)
class Winner:
    target: InventoryTarget
    candidate: CandidateVerdict
    sealed_attempts: tuple[str, ...]


def reduce_attempt_history(
    target: InventoryTarget,
    candidates: Iterable[CandidateVerdict],
) -> Winner:
    rows = list(candidates)
    invalid = [row for row in rows if row.classification == INVALID]
    if invalid:
        details = "; ".join(
            f"{row.bundle.bundle_id}:{','.join(row.errors)}" for row in invalid
        )
        raise FinalizationError(f"invalid evidence for {target.stem}: {details}")
    successes = [row for row in rows if row.classification == SUCCESS]
    if not successes:
        raise FinalizationError(f"no validated success for {target.stem}")
    successes.sort(key=lambda row: (row.event_at, row.bundle.bundle_id))
    winner = successes[-1]
    sealed: list[str] = []
    for row in rows:
        if row is winner or row.classification == SUCCESS:
            continue
        if row.event_at < winner.started_at:
            sealed.append(row.bundle.bundle_id)
            continue
        raise FinalizationError(
            f"unsealed later/overlapping RPC attempt for {target.stem}: "
            f"{row.bundle.bundle_id}"
        )
    return Winner(target, winner, tuple(sorted(sealed)))


@dataclass(frozen=True)
class AuditResult:
    inventory: OrderedInventory
    winners: tuple[Winner, ...]
    missing: tuple[str, ...]
    source_snapshots: tuple[JsonSnapshot, ...]

    @property
    def ready(self) -> bool:
        return not self.missing and len(self.winners) == len(self.inventory.targets)


def audit_runs(
    inventory_path: Path | str,
    log_dir: Path | str,
    run_ids: Iterable[str],
    *,
    expected_count: int = INVENTORY_COUNT,
    allow_abandoned_run_ids: Iterable[str] = (),
    verify_artifacts: bool = True,
) -> AuditResult:
    ordered = load_ordered_inventory(
        inventory_path, expected_count=expected_count
    )
    run_ids = tuple(run_ids)
    fleets = load_fleet_sources(
        log_dir,
        run_ids,
        allow_abandoned_run_ids=allow_abandoned_run_ids,
    )
    targets_by_spm = {target.canonical_spm: target for target in ordered.targets}
    candidates_by_spm: dict[str, list[CandidateVerdict]] = {
        key: [] for key in targets_by_spm
    }
    bundles = discover_exact_bundles(log_dir, run_ids)
    for bundle in bundles:
        manifest_snapshot = read_stable_json(bundle.manifest)
        items = manifest_snapshot.payload.get("items") or []
        if len(items) != 1 or not isinstance(items[0], dict):
            raise FinalizationError(
                f"cannot identify exact bundle SPM: {bundle.manifest}"
            )
        key = canonical_path(items[0].get("queue_id") or "")
        target = targets_by_spm.get(key)
        if target is None:
            raise FinalizationError(
                f"exact root bundle is outside ordered inventory: {bundle.manifest}"
            )
        fleet_result = fleets[bundle.run_id].results_by_spm.get(key)
        candidates_by_spm[key].append(validate_exact_bundle(
            bundle,
            target,
            fleet_result=fleet_result,
            verify_artifacts=verify_artifacts,
        ))
    winners: list[Winner] = []
    missing: list[str] = []
    for target in ordered.targets:
        rows = candidates_by_spm[target.canonical_spm]
        try:
            winners.append(reduce_attempt_history(target, rows))
        except FinalizationError:
            missing.append(target.stem)
    source_snapshots = [ordered.snapshot]
    source_snapshots.extend(source.snapshot for source in fleets.values())
    for rows in candidates_by_spm.values():
        for candidate in rows:
            source_snapshots.extend(candidate.snapshots)
    deduplicated: dict[str, JsonSnapshot] = {}
    for snapshot in source_snapshots:
        key = canonical_path(snapshot.path)
        previous = deduplicated.get(key)
        if previous is not None and previous.sha256 != snapshot.sha256:
            raise FinalizationError(
                f"evidence changed between candidate reads: {snapshot.path}"
            )
        deduplicated[key] = snapshot
    return AuditResult(
        inventory=ordered,
        winners=tuple(winners),
        missing=tuple(missing),
        source_snapshots=tuple(deduplicated.values()),
    )


@dataclass(frozen=True)
class ReceiptPlan:
    target: InventoryTarget
    payload: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True)
class FinalizationPlan:
    finalization_id: str
    inventory_vector_sha256: str
    receipts: tuple[ReceiptPlan, ...]
    source_snapshots: tuple[JsonSnapshot, ...]
    commit_ledger: Path


def build_finalization_plan(
    audit: AuditResult,
    *,
    commit_ledger: Path | str,
    created_at: str | None = None,
) -> FinalizationPlan:
    expected = len(audit.inventory.targets)
    if expected != INVENTORY_COUNT or not audit.ready:
        raise FinalizationError(
            f"80-winner gate failed: {len(audit.winners)}/{expected}"
        )
    winner_ids = [
        {
            "ordinal": winner.target.ordinal,
            "spm": str(winner.target.spm),
            "run_id": winner.candidate.bundle.run_id,
            "fingerprint": winner.candidate.fingerprint,
            "completed_at": winner.candidate.event_at.isoformat(),
        }
        for winner in audit.winners
    ]
    identity = {
        "inventory_vector_sha256": audit.inventory.vector_sha256,
        "winners": winner_ids,
    }
    finalization_id = hashlib.sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()[:24]
    ledger_path = Path(commit_ledger).resolve()
    timestamp = created_at or datetime.now().isoformat(timespec="seconds")
    receipts: list[ReceiptPlan] = []
    for winner in audit.winners:
        candidate = winner.candidate
        evidence = [
            {
                "role": snapshot.path.name,
                "path": str(snapshot.path),
                "sha256": snapshot.sha256,
            }
            for snapshot in candidate.snapshots
        ]
        payload = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "imported_ok",
            "created_at": timestamp,
            "finalization_id": finalization_id,
            "commit_ledger": str(ledger_path),
            "inventory_ordinal": winner.target.ordinal,
            "inventory_vector_sha256": audit.inventory.vector_sha256,
            "spm": str(winner.target.spm),
            "item_fingerprint": candidate.fingerprint,
            "verification_basis": candidate.verification_basis,
            "attempt": {
                "run_id": candidate.bundle.run_id,
                "bundle_id": candidate.bundle.bundle_id,
                "started_at": candidate.started_at.isoformat(),
                "completed_at": candidate.event_at.isoformat(),
                "sealed_attempts": list(winner.sealed_attempts),
            },
            "native_receipt": str(winner.target.native_receipt),
            "native_receipt_sha256": _sha256_file(winner.target.native_receipt),
            "assembly_manifest": str(winner.target.manifest),
            "assembly_manifest_sha256": _sha256_file(winner.target.manifest),
            "cluster_fleet_report": str(candidate.bundle.fleet_report),
            "exact_push_report": (
                str(candidate.bundle.exact_report)
                if candidate.bundle.exact_report.is_file()
                else None
            ),
            "evidence": evidence,
        }
        receipts.append(ReceiptPlan(
            target=winner.target,
            payload=payload,
            payload_sha256=_sha256_bytes(_json_bytes(payload)),
        ))
    if len({canonical_path(row.target.deployment_receipt) for row in receipts}) != 80:
        raise FinalizationError("receipt paths are not one-to-one")
    return FinalizationPlan(
        finalization_id=finalization_id,
        inventory_vector_sha256=audit.inventory.vector_sha256,
        receipts=tuple(receipts),
        source_snapshots=audit.source_snapshots,
        commit_ledger=ledger_path,
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _replace_with_retry(source: Path, target: Path, *, attempts: int = 20) -> None:
    """Preserve atomic replace semantics across transient Windows sharing locks."""
    last_error: PermissionError | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt + 1 < max(1, int(attempts)):
                time.sleep(0.01)
    assert last_error is not None
    raise last_error


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _replace_with_retry(temporary, path)
    _fsync_directory(path.parent)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(path, _json_bytes(payload))


def stage_deployment_receipts(
    plan: FinalizationPlan,
    journal_path: Path | str,
) -> Path:
    if len(plan.receipts) != INVENTORY_COUNT:
        raise FinalizationError("80-winner gate failed before staging")
    for snapshot in plan.source_snapshots:
        verify_snapshot_unchanged(snapshot)
    journal_file = Path(journal_path).resolve()
    journal_rows: list[dict[str, Any]] = []
    for receipt in plan.receipts:
        final_path = receipt.target.deployment_receipt
        stage_path = final_path.with_name(
            f".{final_path.name}.{plan.finalization_id}.stage"
        )
        backup_path = final_path.with_name(
            f".{final_path.name}.{plan.finalization_id}.previous"
        )
        journal_rows.append({
            "ordinal": receipt.target.ordinal,
            "spm": str(receipt.target.spm),
            "path": str(final_path),
            "stage_path": str(stage_path),
            "backup_path": str(backup_path),
            "payload": receipt.payload,
            "payload_sha256": receipt.payload_sha256,
            "state": "planned",
        })
    journal = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "kind": "affected_refresh_deployment_journal",
        "state": "staging",
        "finalization_id": plan.finalization_id,
        "inventory_vector_sha256": plan.inventory_vector_sha256,
        "commit_ledger": str(plan.commit_ledger),
        "sources": [
            {
                "path": str(snapshot.path),
                "sha256": snapshot.sha256,
                "size": snapshot.size,
            }
            for snapshot in plan.source_snapshots
        ] + [
            {
                "path": str(path),
                "sha256": sha256,
                "size": path.stat().st_size,
            }
            for path, sha256 in {
                **{
                    receipt.target.native_receipt:
                    receipt.payload["native_receipt_sha256"]
                    for receipt in plan.receipts
                },
                **{
                    receipt.target.manifest:
                    receipt.payload["assembly_manifest_sha256"]
                    for receipt in plan.receipts
                },
            }.items()
        ],
        "receipts": journal_rows,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write_json(journal_file, journal)
    for row in journal_rows:
        stage_path = Path(row["stage_path"])
        _atomic_write_bytes(stage_path, _json_bytes(row["payload"]))
        if _sha256_file(stage_path) != row["payload_sha256"]:
            raise FinalizationError(f"staged receipt hash mismatch: {stage_path}")
        row["state"] = "staged"
        journal["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(journal_file, journal)
    journal["state"] = "prepared"
    journal["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write_json(journal_file, journal)
    return journal_file


def _verify_journal_sources(journal: dict[str, Any]) -> None:
    for source in journal.get("sources") or []:
        path = Path(source["path"])
        if not path.is_file() or path.stat().st_size != int(source["size"]):
            raise FinalizationError(f"source changed before commit: {path}")
        if _sha256_file(path) != source["sha256"]:
            raise FinalizationError(f"source changed before commit: {path}")


def commit_deployment_receipts(
    journal_path: Path | str,
    *,
    after_install: Callable[[int, Path], None] | None = None,
) -> Path:
    journal_file = Path(journal_path).resolve()
    journal = read_stable_json(journal_file).payload
    if journal.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise FinalizationError("unsupported finalization journal schema")
    rows = journal.get("receipts") or []
    if len(rows) != INVENTORY_COUNT:
        raise FinalizationError("journal does not contain exactly 80 receipts")
    _verify_journal_sources(journal)
    journal["state"] = "committing"
    _atomic_write_json(journal_file, journal)
    for index, row in enumerate(rows, 1):
        final_path = Path(row["path"])
        stage_path = Path(row["stage_path"])
        backup_path = Path(row["backup_path"])
        expected = str(row["payload_sha256"])
        if final_path.is_file() and _sha256_file(final_path) == expected:
            row["state"] = "installed"
            _atomic_write_json(journal_file, journal)
            continue
        if not stage_path.is_file():
            _atomic_write_bytes(stage_path, _json_bytes(row["payload"]))
        if _sha256_file(stage_path) != expected:
            raise FinalizationError(f"staged receipt changed: {stage_path}")
        if final_path.is_file() and not backup_path.is_file():
            _atomic_write_bytes(backup_path, final_path.read_bytes())
        _replace_with_retry(stage_path, final_path)
        _fsync_directory(final_path.parent)
        if _sha256_file(final_path) != expected:
            raise FinalizationError(f"installed receipt hash mismatch: {final_path}")
        row["state"] = "installed"
        journal["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(journal_file, journal)
        if after_install is not None:
            after_install(index, final_path)
    ledger_path = Path(journal["commit_ledger"])
    ledger = {
        "schema_version": COMMIT_LEDGER_SCHEMA_VERSION,
        "kind": "affected_refresh_deployment_commit",
        "status": "committed",
        "finalization_id": journal["finalization_id"],
        "inventory_vector_sha256": journal["inventory_vector_sha256"],
        "receipt_count": INVENTORY_COUNT,
        "receipts": [
            {
                "ordinal": row["ordinal"],
                "spm": row["spm"],
                "path": row["path"],
                "payload_sha256": row["payload_sha256"],
            }
            for row in rows
        ],
        "committed_at": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write_json(ledger_path, ledger)
    journal["state"] = "committed"
    journal["committed_at"] = ledger["committed_at"]
    _atomic_write_json(journal_file, journal)
    return ledger_path


def resume_deployment_commit(journal_path: Path | str) -> Path:
    return commit_deployment_receipts(journal_path)


def schema2_receipt_is_committed(path: Path | str) -> bool:
    try:
        receipt_path = Path(path).resolve()
        receipt_snapshot = read_stable_json(receipt_path)
        receipt = receipt_snapshot.payload
        if (
            receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
            or receipt.get("status") != "imported_ok"
        ):
            return False
        ledger = read_stable_json(receipt["commit_ledger"]).payload
        if (
            ledger.get("status") != "committed"
            or ledger.get("finalization_id") != receipt.get("finalization_id")
            or int(ledger.get("receipt_count") or 0) != INVENTORY_COUNT
        ):
            return False
        key = canonical_path(receipt_path)
        matches = [
            row for row in ledger.get("receipts") or []
            if canonical_path(row.get("path") or "") == key
        ]
        return (
            len(matches) == 1
            and matches[0].get("payload_sha256") == receipt_snapshot.sha256
        )
    except (FinalizationError, OSError, KeyError, TypeError, ValueError):
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit and finalize a completed forced affected refresh"
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--allow-abandoned-run-id", action="append", default=[])
    parser.add_argument("--expected-count", type=int, default=INVENTORY_COUNT)
    parser.add_argument("--no-artifact-hash", action="store_true")
    parser.add_argument("--write-receipts", action="store_true")
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--commit-ledger", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        audit = audit_runs(
            args.inventory,
            args.log_dir,
            args.run_id,
            expected_count=args.expected_count,
            allow_abandoned_run_ids=args.allow_abandoned_run_id,
            verify_artifacts=not args.no_artifact_hash,
        )
        result = {
            "schema_version": 1,
            "status": "ready" if audit.ready else "incomplete",
            "inventory_count": len(audit.inventory.targets),
            "winner_count": len(audit.winners),
            "missing": list(audit.missing),
            "inventory_vector_sha256": audit.inventory.vector_sha256,
            "write_requested": bool(args.write_receipts),
        }
        if args.write_receipts:
            if args.journal is None or args.commit_ledger is None:
                raise FinalizationError(
                    "--write-receipts requires --journal and --commit-ledger"
                )
            plan = build_finalization_plan(
                audit, commit_ledger=args.commit_ledger
            )
            journal = stage_deployment_receipts(plan, args.journal)
            ledger = commit_deployment_receipts(journal)
            result["status"] = "committed"
            result["finalization_id"] = plan.finalization_id
            result["commit_ledger"] = str(ledger)
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            _atomic_write_bytes(args.output.resolve(), (encoded + "\n").encode("utf-8"))
        print(encoded)
        return 0 if audit.ready else 2
    except FinalizationError as exc:
        result = {"schema_version": 1, "status": "blocked", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
