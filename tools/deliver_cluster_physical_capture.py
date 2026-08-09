"""Deliver and verify one Cluster physical capture without either GUI.

This command is intentionally a thin, rerunnable entry point over the same
transaction used by Generator Sync.  It never treats Blender exit success as
delivery success: the finalized manifest, eight maps, normalization receipt,
and all immutable fingerprints are validated before a content-addressed
delivery receipt is committed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cluster_physical_capture_contract import (
    canonical_sha256,
    file_fingerprint,
    validate_normalization_receipt,
    validate_physical_capture_manifest,
)


DELIVERY_KIND = "speedtree_cluster_physical_capture_delivery"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", required=True)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--blender", required=True)
    parser.add_argument(
        "--bwr-addon-dir",
        help=(
            "Path to addons/speedtree_bone_weight_repair when this checkout "
            "does not have the sibling BWR repository"
        ),
    )
    parser.add_argument("--atlas-addon-dir")
    parser.add_argument("--normalizer-addon-dir")
    parser.add_argument("--unit-probe", required=True)
    parser.add_argument("--capture-resolution", type=int, default=1024)
    parser.add_argument("--expected-plane", choices=("XY", "XZ", "YZ"))
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--receipt-dir")
    return parser.parse_args(argv)


def _capture_manifest_path(blend):
    blend = Path(blend).expanduser().absolute()
    stem = blend.stem[3:] if blend.stem.casefold().startswith("sk_") else blend.stem
    return blend.with_name(f"{stem}_auto_capture_manifest.json")


def _unique_targets(values):
    result = []
    seen = set()
    for value in values:
        path = Path(value).expanduser().absolute()
        key = os.path.normcase(str(path)).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def build_delivery_receipt(blend, targets, manifest_evidence, normalization_evidence):
    payload = {
        "kind": DELIVERY_KIND,
        "version": 1,
        "status": "ready",
        "source_issue": 215,
        "blend": file_fingerprint(blend),
        "target_spms": [file_fingerprint(path) for path in targets],
        "capture": {
            "manifest": manifest_evidence["manifest"],
            "contract_sha256": manifest_evidence["contract_sha256"],
            "orientation": manifest_evidence["orientation"],
            "extent": manifest_evidence["extent"],
            "resolution": manifest_evidence["resolution"],
            "map_roles": manifest_evidence["map_roles"],
            "maps": manifest_evidence["maps"],
        },
        "normalization": normalization_evidence,
    }
    payload["delivery_sha256"] = canonical_sha256(payload)
    return payload


def persist_delivery_receipt(payload, directory, blend):
    directory = Path(directory).expanduser().absolute()
    directory.mkdir(parents=True, exist_ok=True)
    blend = Path(blend).expanduser().absolute()
    digest = str(payload["delivery_sha256"])
    hash_payload = {
        key: value for key, value in payload.items() if key != "delivery_sha256"
    }
    if canonical_sha256(hash_payload) != digest:
        raise RuntimeError("Physical-capture delivery receipt hash is stale.")
    path = directory / (
        f"{blend.stem}_physical_capture_delivery_{digest[:16]}.json"
    )
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise RuntimeError(
                    "Content-addressed physical-capture receipt already exists "
                    f"with different bytes: {path}"
                )
            return path, False
        return path, True
    finally:
        temporary.unlink(missing_ok=True)


def deliver(args):
    overrides = (
        (
            args.bwr_addon_dir,
            "SPEEDTREE_BWR_ADDON_DIR",
            "speedtree_bone_weight_repair",
            (
                "speedtree_cli.py",
                "presets/speedtree_10_1/Options_MA_Fbx.ini",
            ),
        ),
        (
            args.atlas_addon_dir,
            "SPEEDTREE_ATLAS_ADDON_DIR",
            "atlas_leaf_mesh_builder",
            (),
        ),
        (
            args.normalizer_addon_dir,
            "SPEEDTREE_CLUSTER_NORMALIZER_ADDON_DIR",
            "speedtree_cluster_normalizer",
            (),
        ),
    )
    for raw_path, environment_name, module_name, extra_files in overrides:
        if not raw_path:
            continue
        addon_dir = Path(raw_path).expanduser().absolute()
        required = (addon_dir / "__init__.py",) + tuple(
            addon_dir / relative for relative in extra_files
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise ValueError(
                f"{module_name} add-on directory is incomplete: "
                + ", ".join(missing)
            )
        os.environ[environment_name] = str(addon_dir)
    # Import the transaction only after the optional BWR source override is
    # installed; sk_batch resolves its preset/helper paths at import time.
    from cluster_blend_sync import run_cluster_relation_transaction
    from cluster_normalization_sync import (
        capture_plane_for_blend,
        normalization_receipt_path,
        resolve_normalization_recipe,
    )

    blend = Path(args.blend).expanduser().absolute()
    targets = _unique_targets(args.target)
    expected_plane = capture_plane_for_blend(blend)
    if args.expected_plane and args.expected_plane != expected_plane:
        raise ValueError(
            f"Requested plane {args.expected_plane} conflicts with exact filename "
            f"token policy {expected_plane}: {blend.name}"
        )

    def progress(_stage, message):
        message = str(message or "")
        if message:
            print(message, file=sys.stderr, flush=True)

    transaction = run_cluster_relation_transaction(
        blend,
        targets,
        enabled=True,
        blender_exe=args.blender,
        unit_probe_path=args.unit_probe,
        capture_resolution=args.capture_resolution,
        auto_normalize=True,
        force_refresh=bool(args.force_refresh),
        progress_callback=progress,
        timeout=args.timeout,
    )
    manifest_evidence = validate_physical_capture_manifest(
        _capture_manifest_path(blend),
        expected_blend=blend,
        expected_plane=expected_plane,
        expected_resolution=args.capture_resolution,
        expected_target_meters=0.1,
        expected_padding_ratio=0.04,
    )
    current_recipe = resolve_normalization_recipe(
        blend,
        targets,
        unit_probe_path=args.unit_probe,
        capture_resolution=args.capture_resolution,
    )
    if current_recipe["normalization_required"]:
        raise RuntimeError(
            "Cluster transaction completed without a current normalization receipt."
        )
    normalization_evidence = validate_normalization_receipt(
        normalization_receipt_path(blend),
        manifest_evidence=manifest_evidence,
        expected_blend=blend,
        expected_normalization_contract_sha256=current_recipe[
            "normalization_contract_sha256"
        ],
    )
    payload = build_delivery_receipt(
        blend,
        targets,
        manifest_evidence,
        normalization_evidence,
    )
    receipt_dir = args.receipt_dir or (blend.parent / "reports")
    receipt_path, written = persist_delivery_receipt(
        payload, receipt_dir, blend
    )
    return {
        "status": "ready",
        "plane": expected_plane,
        "transaction_no_change": bool(transaction.get("no_change")),
        "transaction_refresh_reasons": list(
            transaction.get("refresh_reasons")
            or transaction.get("preflight_refresh_reasons")
            or []
        ),
        "manifest": manifest_evidence["manifest"],
        "physical_capture_contract_sha256": manifest_evidence[
            "contract_sha256"
        ],
        "normalization_receipt": normalization_evidence["receipt"],
        "delivery_receipt": str(receipt_path),
        "delivery_receipt_written": written,
        "delivery_sha256": payload["delivery_sha256"],
    }


def main(argv=None):
    result = deliver(parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
