"""Fail-closed interactive recovery for a stale saved SpeedTree Node table.

The Modeler is opened only after an exact byte preimage and an immutable,
SHA-bound receipt have been created and verified.  The module never edits the
SPM, automates Save, kills Modeler, rolls back automatically, or treats
``stale=false`` alone as permission to continue.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from speedtree_pipeline_contract import (
    SPM_AUTHORING_GRAPH_PROJECTION_VERSION,
    canonical_path_key,
    spm_authoring_graph_fingerprint,
)

try:
    from .pcg_cluster_assembly_contract import _normalized_generator_delivery
    from .pcg_texture_audit import (
        _export_node_counts_from_text,
        generator_delivery_snapshot_from_spm_text,
    )
except ImportError:  # Direct script execution from the tool directory.
    from pcg_cluster_assembly_contract import _normalized_generator_delivery
    from pcg_texture_audit import (
        _export_node_counts_from_text,
        generator_delivery_snapshot_from_spm_text,
    )


SHA256_RE = re.compile(r"[0-9a-f]{64}")
RECOVERY_CONTRACT = "speedtree_stale_node_table_interactive_recovery_v2"
PREIMAGE_RECEIPT_KIND = "speedtree_stale_node_table_preimage_receipt"
BLOCKED_EVENT_KIND = "speedtree_stale_node_table_recovery_blocked"
CONTINUATION_CLAIM_KIND = "speedtree_stale_node_table_continuation_claim"


class StaleNodeTableRecoveryError(RuntimeError):
    """The interactive recovery failed closed with stable public evidence."""

    def __init__(self, reason_token, message, evidence=None):
        super().__init__(message)
        self.reason_token = str(reason_token)
        self.evidence = dict(evidence or {})

    def public_payload(self):
        return {
            "status": "blocked",
            "contract": RECOVERY_CONTRACT,
            "reason_token": self.reason_token,
            "evidence": dict(self.evidence),
        }


class StaleNodeTableRecoveryTimeout(StaleNodeTableRecoveryError):
    """The watched SPM did not reach a stable valid state in time."""


def _canonical_json_bytes(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _json_fingerprint(value):
    return _sha256_bytes(_canonical_json_bytes(value))


def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]


def _mesh_id(value):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _source_identity(spm):
    return {
        "asset_name": Path(spm).name,
        "source_identity_sha256": hashlib.sha256(
            canonical_path_key(spm).encode("utf-8")
        ).hexdigest(),
    }


def _public_hash_evidence(snapshot=None, **extra):
    evidence = {}
    if snapshot:
        evidence.update({
            "asset_name": snapshot["source_identity"]["asset_name"],
            "source_identity_sha256": snapshot["source_identity"][
                "source_identity_sha256"
            ],
            "after_sha256": snapshot.get("text_sha256"),
            "after_raw_sha256": snapshot.get("raw_sha256"),
        })
    evidence.update({key: value for key, value in extra.items() if value is not None})
    return evidence


def _decode_spm_bytes(raw_bytes):
    decoded = gzip.decompress(raw_bytes) if raw_bytes[:2] == b"\x1f\x8b" else raw_bytes
    return decoded.decode("utf-8", errors="strict")


def _stat_identity(stat):
    return (
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def _child_text(element, name):
    expected = str(name).casefold()
    for child in element:
        if _local_name(child.tag).casefold() == expected:
            return "".join(child.itertext()).strip()
    return ""


def _truthy(value):
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _elementtree_node_evidence(text):
    """Independent Node/Generator counts from the same immutable XML text."""
    root = ET.fromstring(text)
    generator_guids = set()
    eligible = {}
    total = 0
    for element in root.iter():
        tag = _local_name(element.tag).casefold()
        if tag == "generator":
            guid = _child_text(element, "GUID").casefold()
            if guid:
                generator_guids.add(guid)
        elif tag == "node":
            total += 1
            guid = _child_text(element, "GeneratorGUID").casefold()
            hidden = _truthy(_child_text(element, "Hidden"))
            deleted = False
            culled = False
            for descendant in element.iter():
                descendant_tag = _local_name(descendant.tag).casefold()
                if descendant_tag == "m_bdeleted":
                    deleted = _truthy("".join(descendant.itertext()))
                elif descendant_tag == "m_bculled":
                    culled = _truthy("".join(descendant.itertext()))
            if guid and not hidden and not deleted and not culled:
                eligible[guid] = eligible.get(guid, 0) + 1
    orphan_guids = sorted(set(eligible) - generator_guids)
    return {
        "total_node_count": total,
        "generator_count": len(generator_guids),
        "eligible_owner_count": len(eligible),
        "eligible_node_count": sum(eligible.values()),
        "orphan_owner_count": len(orphan_guids),
        "orphan_node_count": sum(eligible[guid] for guid in orphan_guids),
        "eligible_owner_counts_fingerprint": _json_fingerprint(
            sorted(eligible.items())
        ),
        "generator_membership_fingerprint": _json_fingerprint(
            sorted(generator_guids)
        ),
        "_eligible_counts": eligible,
        "_generator_guids": generator_guids,
    }


def _target_binding_projection(snapshot, expected_mesh_ids):
    expected = sorted({_mesh_id(value) for value in expected_mesh_ids} - {None})
    expected_set = set(expected)
    rows = []
    for row in snapshot.get("leaf_generator_bindings") or []:
        mesh_id = _mesh_id(row.get("mesh_id"))
        if mesh_id not in expected_set:
            continue
        rows.append({
            "generator_guid": str(row.get("generator_guid") or "").strip().casefold(),
            "generator_type": str(row.get("generator_type") or "").strip(),
            "generator_name": str(row.get("generator_name") or "").strip(),
            "slot_prefix": str(row.get("slot_prefix") or "").strip(),
            "material_property": str(row.get("material_property") or "").strip(),
            "material_id": _mesh_id(row.get("material_id")),
            "mesh_property": str(row.get("mesh_property") or "").strip(),
            "mesh_id": mesh_id,
        })
    rows.sort(key=lambda row: _canonical_json_bytes(row))
    observed = sorted({row["mesh_id"] for row in rows})
    return {
        "expected_target_mesh_ids": expected,
        "observed_target_mesh_ids": observed,
        "binding_count": len(rows),
        "complete": bool(expected and observed == expected),
        "fingerprint": _json_fingerprint(rows),
        "_rows": rows,
    }


class _FrozenAudit:
    def __init__(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)

    def live_generator_delivery_snapshot(self, _path):
        return copy.deepcopy(self.snapshot)


def _normalization_evidence(snapshot, target_projection):
    """Exercise the production normalization classifier on frozen bytes."""
    rows = target_projection["_rows"]
    material_ids = sorted({row["material_id"] for row in rows if row["material_id"]})
    if len(material_ids) != 1:
        return {
            "delivery_mode": "CONNECTION_INCOMPLETE",
            "delivery_decision": "blocked",
            "delivery_reason": "recovery_target_material_scope_ambiguous",
            "errors": ["recovery_target_material_scope_ambiguous"],
            "complete": False,
        }
    declared = []
    for row in rows:
        declared_row = dict(row)
        declared_row["target_material_id"] = row["material_id"]
        declared_row["target_mesh_id"] = row["mesh_id"]
        declared.append(declared_row)
    payload = {
        "generator_connection": {
            "requested": True,
            "complete": True,
            "generator_variant_policy": "ensure_all_material_cutouts",
            "bindings": declared,
        }
    }
    variants = [
        {"target_mesh_id": mesh_id}
        for mesh_id in target_projection["expected_target_mesh_ids"]
    ]
    evidence = _normalized_generator_delivery(
        _FrozenAudit(snapshot),
        snapshot["spm"],
        payload,
        {"material_id": material_ids[0]},
        variants,
    )
    return {
        "delivery_mode": evidence.get("delivery_mode"),
        "delivery_decision": evidence.get("delivery_decision"),
        "delivery_reason": evidence.get("delivery_reason"),
        "live_snapshot_sha256": evidence.get("live_snapshot_sha256"),
        "errors": list(evidence.get("errors") or []),
        "complete": bool(
            evidence.get("delivery_mode") == "render_connected"
            and evidence.get("delivery_decision") == "normalize_part"
            and evidence.get("generator_connection_complete") is True
            and not evidence.get("errors")
        ),
    }


def _capture_immutable_snapshot(spm_path, expected_mesh_ids):
    """Capture stat+bytes+parse evidence without re-reading for sub-audits."""
    spm = Path(spm_path)
    try:
        before = spm.stat()
        raw_bytes = spm.read_bytes()
        after = spm.stat()
    except OSError as exc:
        raise StaleNodeTableRecoveryError(
            "source_snapshot_read_failed",
            "the operating SPM could not be captured",
            _source_identity(spm),
        ) from exc
    if _stat_identity(before) != _stat_identity(after) or len(raw_bytes) != after.st_size:
        raise StaleNodeTableRecoveryError(
            "source_changed_during_capture",
            "the operating SPM changed during immutable capture",
            _source_identity(spm),
        )
    try:
        text = _decode_spm_bytes(raw_bytes)
        ET.fromstring(text)
        delivery = generator_delivery_snapshot_from_spm_text(text, spm)
        regex_counts, regex_total = _export_node_counts_from_text(text)
        elementtree = _elementtree_node_evidence(text)
        target_projection = _target_binding_projection(delivery, expected_mesh_ids)
        normalization = _normalization_evidence(delivery, target_projection)
        authoring_fingerprint = spm_authoring_graph_fingerprint(text)
    except (ET.ParseError, OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise StaleNodeTableRecoveryError(
            "source_snapshot_parse_failed",
            "the immutable SPM snapshot could not be parsed and audited",
            _source_identity(spm),
        ) from exc
    regex_summary = {
        "total_node_count": regex_total,
        "eligible_owner_count": len(regex_counts),
        "eligible_node_count": sum(regex_counts.values()),
        "eligible_owner_counts_fingerprint": _json_fingerprint(
            sorted(regex_counts.items())
        ),
    }
    parity = bool(
        regex_total == elementtree["total_node_count"]
        and regex_counts == elementtree["_eligible_counts"]
    )
    return {
        "source_identity": _source_identity(spm),
        "raw_bytes": raw_bytes,
        "text": text,
        "raw_sha256": _sha256_bytes(raw_bytes),
        "text_sha256": delivery["spm_text_sha256"],
        "size": len(raw_bytes),
        "mtime_ns": int(after.st_mtime_ns),
        "stat_identity": _stat_identity(after),
        "quiescence_key": (
            int(after.st_size),
            int(after.st_mtime_ns),
            _sha256_bytes(raw_bytes),
        ),
        "delivery": delivery,
        "regex": regex_summary,
        "elementtree": elementtree,
        "regex_elementtree_parity": parity,
        "authoring_graph_fingerprint": authoring_fingerprint,
        "generator_membership_fingerprint": elementtree[
            "generator_membership_fingerprint"
        ],
        "target_projection": target_projection,
        "normalization": normalization,
    }


def validate_repaired_snapshot(snapshot, expected_mesh_ids=()):
    """Return a fail-closed target/Node-table verdict for frozen evidence."""
    expected = sorted({_mesh_id(value) for value in expected_mesh_ids} - {None})
    errors = []
    node_table = snapshot.get("node_table") or {}
    if node_table.get("stale") is not False:
        errors.append("node_table_still_stale")
    if int(node_table.get("orphan_node_count") or 0):
        errors.append("orphan_nodes_remain")
    if node_table.get("orphan_generator_guids"):
        errors.append("orphan_owners_remain")

    expected_set = set(expected)
    target_rows = [
        dict(row)
        for row in snapshot.get("leaf_generator_bindings") or []
        if _mesh_id(row.get("mesh_id")) in expected_set
    ]
    observed = sorted({_mesh_id(row.get("mesh_id")) for row in target_rows} - {None})
    live = sorted({
        _mesh_id(row.get("mesh_id"))
        for row in target_rows
        if row.get("export_participates") is True
    } - {None})
    if not expected:
        errors.append("expected_target_mesh_ids_missing")
    if observed != expected:
        errors.append("required_target_binding_missing")
    if live != expected:
        errors.append("live_target_mesh_set_incomplete")
    for row in target_rows:
        if row.get("graph_visible") is not True:
            errors.append("target_binding_not_graph_visible")
        if int(row.get("generated_node_count") or 0) <= 0:
            errors.append("target_binding_has_no_eligible_nodes")
        if row.get("export_participates") is not True:
            errors.append("target_binding_not_export_participating")
        if row.get("export_evidence") != "node_table":
            errors.append("target_binding_evidence_not_current_node_table")
        if row.get("node_table_stale") is not False:
            errors.append("target_binding_reports_stale_node_table")
    return {
        "contract": RECOVERY_CONTRACT,
        "valid": not errors,
        "errors": sorted(set(errors)),
        "spm_text_sha256": snapshot.get("spm_text_sha256"),
        "node_table": {
            "generator_count": node_table.get("generator_count"),
            "node_table_generator_count": node_table.get("node_table_generator_count"),
            "orphan_generator_guid_count": len(
                node_table.get("orphan_generator_guids") or []
            ),
            "orphan_node_count": int(node_table.get("orphan_node_count") or 0),
            "total_node_count": node_table.get("total_node_count"),
            "stale": node_table.get("stale"),
        },
        "expected_target_mesh_ids": expected,
        "observed_target_mesh_ids": observed,
        "live_export_participating_target_mesh_ids": live,
        "target_binding_count": len(target_rows),
    }


def _snapshot_gate(snapshot, preimage_receipt, expected_mesh_ids):
    errors = list(
        validate_repaired_snapshot(snapshot["delivery"], expected_mesh_ids)["errors"]
    )
    if snapshot["regex_elementtree_parity"] is not True:
        errors.append("regex_elementtree_node_evidence_mismatch")
    if snapshot["authoring_graph_fingerprint"] != preimage_receipt[
        "authoring_graph_projection"
    ]["fingerprint"]:
        errors.append("authoring_graph_changed_during_resave")
    if snapshot["generator_membership_fingerprint"] != preimage_receipt[
        "generator_membership"
    ]["fingerprint"]:
        errors.append("generator_membership_changed_during_resave")
    if snapshot["target_projection"]["fingerprint"] != preimage_receipt[
        "required_target_bindings"
    ]["fingerprint"]:
        errors.append("required_target_bindings_changed_during_resave")
    if snapshot["normalization"]["complete"] is not True:
        errors.append("normalization_evidence_not_complete")
    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "after_sha256": snapshot["text_sha256"],
        "after_raw_sha256": snapshot["raw_sha256"],
        "regex_elementtree_parity": snapshot["regex_elementtree_parity"],
        "authoring_graph_continuity": (
            snapshot["authoring_graph_fingerprint"]
            == preimage_receipt["authoring_graph_projection"]["fingerprint"]
        ),
        "generator_membership_continuity": (
            snapshot["generator_membership_fingerprint"]
            == preimage_receipt["generator_membership"]["fingerprint"]
        ),
        "required_target_binding_continuity": (
            snapshot["target_projection"]["fingerprint"]
            == preimage_receipt["required_target_bindings"]["fingerprint"]
        ),
        "target_delivery": validate_repaired_snapshot(
            snapshot["delivery"], expected_mesh_ids
        ),
        "normalization": dict(snapshot["normalization"]),
    }


def _atomic_write_new(path, payload):
    """Write one new artifact atomically; never replace an existing target."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    data = payload if isinstance(payload, bytes) else _canonical_json_bytes(payload)
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if path.exists():
            raise FileExistsError(str(path))
        os.rename(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _acquire_session_lock(recovery_root, source_identity):
    lock = recovery_root / (
        "session." + source_identity["source_identity_sha256"][:24] + ".lock.json"
    )
    payload = {
        "kind": "speedtree_stale_node_table_recovery_session_lock",
        "schema_version": 1,
        **source_identity,
        "session_token": uuid.uuid4().hex,
    }
    try:
        _atomic_write_new(lock, payload)
    except FileExistsError as exc:
        raise StaleNodeTableRecoveryError(
            "recovery_session_already_active",
            "another recovery session or an interrupted session lock exists",
            source_identity,
        ) from exc
    return lock, payload["session_token"]


def _release_session_lock(lock, token):
    try:
        current = json.loads(lock.read_text(encoding="utf-8"))
        if current.get("session_token") == token:
            lock.unlink()
    except (OSError, UnicodeError, json.JSONDecodeError):
        # Never delete an unverified lock belonging to another/interrupted run.
        pass


def _preimage_receipt(snapshot, expected_mesh_ids, backup_name):
    target = snapshot["target_projection"]
    return {
        "kind": PREIMAGE_RECEIPT_KIND,
        "schema_version": 2,
        "recovery_contract": RECOVERY_CONTRACT,
        **snapshot["source_identity"],
        "exact_preimage": {
            "raw_sha256": snapshot["raw_sha256"],
            "spm_text_sha256": snapshot["text_sha256"],
            "size": snapshot["size"],
            "backup_file": backup_name,
            "backup_raw_sha256": snapshot["raw_sha256"],
        },
        "authoring_graph_projection": {
            "contract": "speedtree_spm_authoring_graph_projection",
            "version": SPM_AUTHORING_GRAPH_PROJECTION_VERSION,
            "fingerprint": snapshot["authoring_graph_fingerprint"],
            "excluded": [
                "generated Nodes table",
                "Thumbnail",
                "ThumbnailSize",
                "Preview",
                "Statistics",
                "QuickSaveSettings2",
                "m_sTimelineData",
            ],
        },
        "generator_membership": {
            "contract": "speedtree_generator_membership_projection",
            "version": 1,
            "count": snapshot["elementtree"]["generator_count"],
            "fingerprint": snapshot["generator_membership_fingerprint"],
        },
        "required_target_bindings": {
            "contract": "speedtree_required_target_binding_projection",
            "version": 1,
            "expected_mesh_ids": sorted(expected_mesh_ids),
            "binding_count": target["binding_count"],
            "fingerprint": target["fingerprint"],
        },
        "same_preimage_evidence": {
            "regex_elementtree_parity": snapshot["regex_elementtree_parity"],
            "regex": dict(snapshot["regex"]),
            "elementtree": {
                key: value
                for key, value in snapshot["elementtree"].items()
                if not key.startswith("_")
            },
            "target_manifest_complete": target["complete"],
            "normalization": dict(snapshot["normalization"]),
        },
        "safety_boundary": {
            "modeler_save_automation": False,
            "modeler_process_kill": False,
            "ui_keystroke_simulation": False,
            "direct_spm_xml_mutation": False,
            "automatic_rollback": False,
            "stale_false_alone_allows_continuation": False,
        },
    }


def _verify_preimage_artifacts(artifacts, snapshot=None):
    receipt = artifacts["receipt"]
    expected_raw_sha = receipt["exact_preimage"]["raw_sha256"]
    try:
        backup_bytes = artifacts["backup_path"].read_bytes()
        receipt_bytes = artifacts["receipt_path"].read_bytes()
        receipt_on_disk = json.loads(receipt_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StaleNodeTableRecoveryError(
            "preimage_artifacts_missing_or_unreadable",
            "the exact backup or immutable receipt is missing or unreadable",
            _public_hash_evidence(snapshot) if snapshot else {},
        ) from exc
    if _sha256_bytes(backup_bytes) != expected_raw_sha:
        raise StaleNodeTableRecoveryError(
            "preimage_backup_verification_failed",
            "the immutable preimage backup no longer matches its receipt",
            _public_hash_evidence(snapshot) if snapshot else {},
        )
    if receipt_on_disk != receipt:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt no longer matches sealed evidence",
            _public_hash_evidence(snapshot) if snapshot else {},
        )
    receipt_sha256 = _sha256_bytes(receipt_bytes)
    expected_receipt_sha256 = artifacts.get("receipt_sha256")
    if (
        expected_receipt_sha256 is not None
        and receipt_sha256 != expected_receipt_sha256
    ):
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt SHA changed",
            _public_hash_evidence(snapshot) if snapshot else {},
        )
    return receipt_sha256


def _ensure_preimage_artifacts(snapshot, expected_mesh_ids, recovery_root):
    base = f"{Path(snapshot['source_identity']['asset_name']).stem}.{snapshot['raw_sha256']}"
    backup = recovery_root / (base + ".preimage.spm")
    receipt_path = recovery_root / (base + ".receipt.json")
    expected_receipt = _preimage_receipt(snapshot, expected_mesh_ids, backup.name)

    if backup.exists():
        if _sha256_bytes(backup.read_bytes()) != snapshot["raw_sha256"]:
            raise StaleNodeTableRecoveryError(
                "preimage_backup_verification_failed",
                "the immutable preimage backup does not match its source SHA",
                _public_hash_evidence(snapshot),
            )
    else:
        _atomic_write_new(backup, snapshot["raw_bytes"])
    if _sha256_bytes(backup.read_bytes()) != snapshot["raw_sha256"]:
        raise StaleNodeTableRecoveryError(
            "preimage_backup_verification_failed",
            "the exact preimage backup failed post-write verification",
            _public_hash_evidence(snapshot),
        )

    if receipt_path.exists():
        try:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StaleNodeTableRecoveryError(
                "preimage_receipt_verification_failed",
                "the immutable preimage receipt is unreadable",
                _public_hash_evidence(snapshot),
            ) from exc
        if existing != expected_receipt:
            raise StaleNodeTableRecoveryError(
                "preimage_receipt_verification_failed",
                "the immutable preimage receipt does not match the exact snapshot",
                _public_hash_evidence(snapshot),
            )
    else:
        _atomic_write_new(receipt_path, expected_receipt)
    receipt_bytes = receipt_path.read_bytes()
    if json.loads(receipt_bytes.decode("utf-8")) != expected_receipt:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt failed post-write verification",
            _public_hash_evidence(snapshot),
        )
    artifacts = {
        "backup_path": backup,
        "receipt_path": receipt_path,
        "receipt": expected_receipt,
        "receipt_sha256": _sha256_bytes(receipt_bytes),
    }
    _verify_preimage_artifacts(artifacts, snapshot)
    return artifacts


def launch_modeler_for_manual_save(speedtree_exe, spm_path):
    """Open a visible Modeler session without shell, Save, or input automation."""
    return subprocess.Popen(
        [str(speedtree_exe), str(spm_path)],
        cwd=str(Path(spm_path).parent),
        stdin=subprocess.DEVNULL,
    )


def wait_for_valid_resave(
    spm_path,
    preimage_snapshot,
    preimage_receipt,
    expected_mesh_ids=(),
    *,
    timeout=7200,
    poll_interval=2.0,
    stable_reads=3,
    capture_fn=_capture_immutable_snapshot,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
):
    """Require repeated stat/size/SHA/parse quiescence and every safety gate."""
    if timeout <= 0 or poll_interval <= 0 or stable_reads < 2:
        raise StaleNodeTableRecoveryError(
            "invalid_quiescence_configuration",
            "timeout/poll interval must be positive and stable reads at least two",
            preimage_snapshot["source_identity"],
        )
    deadline = monotonic_fn() + float(timeout)
    candidate_key = None
    candidate_reads = 0
    last_errors = ["file_content_not_changed"]
    last_snapshot = None
    while monotonic_fn() < deadline:
        try:
            snapshot = capture_fn(spm_path, expected_mesh_ids)
        except StaleNodeTableRecoveryError as exc:
            last_errors = [exc.reason_token]
            sleep_fn(poll_interval)
            continue
        last_snapshot = snapshot
        if snapshot["raw_sha256"] == preimage_snapshot["raw_sha256"]:
            candidate_key = None
            candidate_reads = 0
            last_errors = ["file_content_not_changed"]
            sleep_fn(poll_interval)
            continue
        if snapshot["quiescence_key"] == candidate_key:
            candidate_reads += 1
        else:
            candidate_key = snapshot["quiescence_key"]
            candidate_reads = 1
        verdict = _snapshot_gate(snapshot, preimage_receipt, expected_mesh_ids)
        last_errors = verdict["errors"]
        if candidate_reads >= stable_reads and verdict["valid"]:
            return snapshot, verdict
        sleep_fn(poll_interval)
    evidence = _public_hash_evidence(last_snapshot or preimage_snapshot)
    evidence["last_reason_tokens"] = sorted(set(last_errors))
    raise StaleNodeTableRecoveryTimeout(
        "valid_resave_quiescence_timeout",
        "the SPM did not reach a stable, valid, graph-continuous resave",
        evidence,
    )


def _validate_retry_contract(retry, job_id, job_generation, guards, identity):
    if retry is None:
        return
    missing = []
    if not str(job_id or "").strip():
        missing.append("job_id")
    if job_generation is None or str(job_generation).strip() == "":
        missing.append("job_generation")
    for name in ("is_cancelled", "is_app_open", "is_job_current"):
        if not callable((guards or {}).get(name)):
            missing.append(name)
    if missing:
        raise StaleNodeTableRecoveryError(
            "continuation_context_incomplete",
            "retry requires initiating job/generation and all lifecycle guards",
            {**identity, "missing_fields": sorted(missing)},
        )


def _check_guards(guards, identity):
    if not guards:
        return
    try:
        if guards["is_cancelled"]():
            token = "initiating_job_cancelled"
        elif not guards["is_app_open"]():
            token = "initiating_app_closed"
        elif not guards["is_job_current"]():
            token = "initiating_job_generation_stale"
        else:
            return
    except Exception as exc:
        raise StaleNodeTableRecoveryError(
            "continuation_guard_failed",
            "a continuation lifecycle guard raised an exception",
            identity,
        ) from exc
    raise StaleNodeTableRecoveryError(
        token,
        "the initiating job is no longer eligible for continuation",
        identity,
    )


def _claim_and_resume_once(
    spm,
    after,
    verdict,
    artifacts,
    recovery_root,
    retry,
    job_id,
    job_generation,
    guards,
    expected_mesh_ids,
    capture_fn,
):
    _check_guards(guards, after["source_identity"])
    _verify_preimage_artifacts(artifacts, after)
    current = capture_fn(spm, expected_mesh_ids)
    if (
        current["raw_sha256"] != after["raw_sha256"]
        or current["text_sha256"] != after["text_sha256"]
    ):
        raise StaleNodeTableRecoveryError(
            "source_changed_before_continuation",
            "the source SHA changed after validation and before continuation",
            _public_hash_evidence(current, verified_after_sha256=after["text_sha256"]),
        )
    current_verdict = _snapshot_gate(
        current,
        artifacts["receipt"],
        expected_mesh_ids,
    )
    if not current_verdict["valid"]:
        raise StaleNodeTableRecoveryError(
            "source_revalidation_failed_before_continuation",
            "the source no longer passes the frozen recovery gates",
            _public_hash_evidence(current, reason_tokens=current_verdict["errors"]),
        )
    job_key = _json_fingerprint({
        "job_id": str(job_id),
        "job_generation": str(job_generation),
        "source_identity_sha256": after["source_identity"]["source_identity_sha256"],
        "verified_after_raw_sha256": after["raw_sha256"],
    })
    claim = recovery_root / ("continuation." + job_key + ".claim.json")
    claim_payload = {
        "kind": CONTINUATION_CLAIM_KIND,
        "schema_version": 1,
        **after["source_identity"],
        "job_identity_sha256": _sha256_bytes(str(job_id).encode("utf-8")),
        "job_generation": str(job_generation),
        "verified_after_sha256": after["text_sha256"],
        "verified_after_raw_sha256": after["raw_sha256"],
        "preimage_receipt_sha256": artifacts["receipt_sha256"],
    }
    try:
        _atomic_write_new(claim, claim_payload)
    except FileExistsError as exc:
        raise StaleNodeTableRecoveryError(
            "continuation_already_claimed",
            "this job/generation/after-SHA continuation was already claimed",
            _public_hash_evidence(after, job_generation=str(job_generation)),
        ) from exc
    _check_guards(guards, after["source_identity"])
    continuation = {
        "contract": RECOVERY_CONTRACT,
        "asset_name": after["source_identity"]["asset_name"],
        "source_identity_sha256": after["source_identity"]["source_identity_sha256"],
        "job_identity_sha256": claim_payload["job_identity_sha256"],
        "job_generation": str(job_generation),
        "verified_after_sha256": after["text_sha256"],
        "verified_after_raw_sha256": after["raw_sha256"],
        "preimage_receipt_sha256": artifacts["receipt_sha256"],
        "recovery_verdict": copy.deepcopy(verdict),
    }
    try:
        return retry(continuation), claim.name
    except Exception as exc:
        raise StaleNodeTableRecoveryError(
            "continuation_callback_failed",
            "the once-only continuation callback failed and will not be replayed",
            _public_hash_evidence(after, job_generation=str(job_generation)),
        ) from exc


def _record_blocked_event(recovery_root, identity, error):
    evidence = dict(error.evidence)
    payload = {
        "kind": BLOCKED_EVENT_KIND,
        "schema_version": 1,
        "recovery_contract": RECOVERY_CONTRACT,
        "status": "blocked",
        "reason_token": error.reason_token,
        "asset_name": evidence.get("asset_name") or identity["asset_name"],
        "source_identity_sha256": (
            evidence.get("source_identity_sha256")
            or identity["source_identity_sha256"]
        ),
        "after_sha256": evidence.get("after_sha256"),
        "after_raw_sha256": evidence.get("after_raw_sha256"),
        "verified_after_sha256": evidence.get("verified_after_sha256"),
        "last_reason_tokens": sorted(evidence.get("last_reason_tokens") or []),
    }
    path = recovery_root / ("blocked." + uuid.uuid4().hex + ".json")
    try:
        _atomic_write_new(path, payload)
        error.evidence["blocked_event"] = path.name
        error.evidence["blocked_event_sha256"] = _sha256_bytes(path.read_bytes())
    except (OSError, FileExistsError):
        error.evidence["blocked_event_write_failed"] = True


def recover_stale_node_table(
    spm_path,
    speedtree_exe,
    expected_mesh_ids=(),
    *,
    timeout=7200,
    poll_interval=2.0,
    stable_reads=3,
    retry=None,
    job_id=None,
    job_generation=None,
    guards=None,
    recovery_root=None,
    capture_fn=_capture_immutable_snapshot,
    launch_fn=launch_modeler_for_manual_save,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
):
    """Seal, open, watch, audit, and resume a bound job at most once."""
    spm = Path(spm_path).expanduser().resolve(strict=False)
    executable = Path(speedtree_exe).expanduser().resolve(strict=False)
    identity = _source_identity(spm)
    if not spm.is_file() or spm.suffix.casefold() != ".spm":
        raise StaleNodeTableRecoveryError(
            "recovery_target_invalid",
            "the recovery target is not an existing SPM",
            identity,
        )
    if not executable.is_file():
        raise StaleNodeTableRecoveryError(
            "speedtree_modeler_executable_missing",
            "the configured SpeedTree Modeler executable is unavailable",
            identity,
        )
    expected = sorted({_mesh_id(value) for value in expected_mesh_ids} - {None})
    if not expected:
        raise StaleNodeTableRecoveryError(
            "expected_target_mesh_ids_missing",
            "recovery requires an explicit target Mesh-ID set",
            identity,
        )
    _validate_retry_contract(
        retry,
        job_id,
        job_generation,
        guards,
        identity,
    )
    root = (
        Path(recovery_root).expanduser().resolve(strict=False)
        if recovery_root is not None
        else spm.parent / "_spm_backups" / "stale_node_table_recovery"
    )
    root.mkdir(parents=True, exist_ok=True)
    lock = None
    token = None
    try:
        lock, token = _acquire_session_lock(root, identity)
        baseline = capture_fn(spm, expected)
        baseline_verdict = validate_repaired_snapshot(baseline["delivery"], expected)
        node_table = baseline["delivery"].get("node_table") or {}
        if node_table.get("stale") is not True:
            if not baseline_verdict["valid"]:
                raise StaleNodeTableRecoveryError(
                    "non_stale_delivery_gate_failed",
                    "the Node table is not stale but target delivery is invalid",
                    _public_hash_evidence(
                        baseline,
                        reason_tokens=baseline_verdict["errors"],
                    ),
                )
            return {
                "contract": RECOVERY_CONTRACT,
                "status": "already_repaired",
                **identity,
                "after_sha256": baseline["text_sha256"],
                "after_raw_sha256": baseline["raw_sha256"],
                "modeler_launched": False,
                "retry_invoked": False,
                "closure_gate": "operational_snapshot_valid_only",
            }
        if not baseline["regex_elementtree_parity"]:
            raise StaleNodeTableRecoveryError(
                "preimage_regex_elementtree_mismatch",
                "the same-byte preimage audits disagree",
                _public_hash_evidence(baseline),
            )
        if not baseline["target_projection"]["complete"]:
            raise StaleNodeTableRecoveryError(
                "preimage_target_manifest_incomplete",
                "the exact preimage does not contain the required target manifest",
                _public_hash_evidence(baseline),
            )
        artifacts = _ensure_preimage_artifacts(baseline, expected, root)
        # The exact source must still be the sealed preimage before Modeler is
        # opened; receipt creation is not authority if the source raced it.
        prelaunch = capture_fn(spm, expected)
        if (
            prelaunch["raw_sha256"] != baseline["raw_sha256"]
            or prelaunch["text_sha256"] != baseline["text_sha256"]
        ):
            raise StaleNodeTableRecoveryError(
                "source_changed_before_modeler_launch",
                "the source changed after preimage sealing and before launch",
                _public_hash_evidence(prelaunch),
            )
        _verify_preimage_artifacts(artifacts, prelaunch)
        _check_guards(guards, identity)
        process = launch_fn(executable, spm)
        after, verdict = wait_for_valid_resave(
            spm,
            baseline,
            artifacts["receipt"],
            expected,
            timeout=timeout,
            poll_interval=poll_interval,
            stable_reads=stable_reads,
            capture_fn=capture_fn,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        retry_result = None
        claim_name = None
        if retry is not None:
            retry_result, claim_name = _claim_and_resume_once(
                spm,
                after,
                verdict,
                artifacts,
                root,
                retry,
                job_id,
                job_generation,
                guards,
                expected,
                capture_fn,
            )
        return {
            "contract": RECOVERY_CONTRACT,
            "status": (
                "repaired_reaudited_and_retried_once"
                if retry is not None
                else "repaired_reaudit_valid"
            ),
            **identity,
            "preimage_sha256": baseline["text_sha256"],
            "preimage_raw_sha256": baseline["raw_sha256"],
            "after_sha256": after["text_sha256"],
            "after_raw_sha256": after["raw_sha256"],
            "preimage_backup": artifacts["backup_path"].name,
            "preimage_receipt": artifacts["receipt_path"].name,
            "preimage_receipt_sha256": artifacts["receipt_sha256"],
            "modeler_launched": True,
            "modeler_process_observed_only": process is not None,
            "reaudit": verdict,
            "retry_invoked": retry is not None,
            "continuation_claim": claim_name,
            "retry_result": retry_result,
            "closure_gate": "cluster_normalization_and_unreal_push_pending",
        }
    except StaleNodeTableRecoveryError as exc:
        _record_blocked_event(root, identity, exc)
        raise
    finally:
        if lock is not None and token is not None:
            _release_session_lock(lock, token)


def configured_speedtree_exe():
    """Use the existing SK Batch SpeedTree configuration."""
    from sk_batch.sk_common import load_config

    return Path(load_config().get("speedtree_exe") or "")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Seal a stale SPM preimage, open it in SpeedTree Modeler, wait "
            "for a manual save, and run a graph-continuous frozen audit."
        )
    )
    parser.add_argument("spm", nargs="+", help="Affected operating SPM path")
    parser.add_argument(
        "--speedtree-exe",
        help="Modeler executable; defaults to the existing SK Batch config",
    )
    parser.add_argument(
        "--expected-mesh-id",
        action="append",
        type=int,
        required=True,
        help="Required live target Mesh ID; repeat for each target",
    )
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--stable-reads", type=int, default=3)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    speedtree_exe = (
        Path(args.speedtree_exe)
        if args.speedtree_exe
        else configured_speedtree_exe()
    )
    results = []
    try:
        for spm in args.spm:
            results.append(
                recover_stale_node_table(
                    spm,
                    speedtree_exe,
                    args.expected_mesh_id,
                    timeout=args.timeout,
                    poll_interval=args.poll_interval,
                    stable_reads=args.stable_reads,
                )
            )
    except StaleNodeTableRecoveryError as exc:
        print(json.dumps(exc.public_payload(), indent=2, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "ok", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
