"""Select a single Blender FBX -> SpeedTree unit contract from real exports.

The caller performs the scratch SPM builds and SpeedTree XML exports.  This
module evaluates those measurements without any tree- or role-specific scale
rules and emits the receipt consumed by Atlas production builds.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path


KIND = "speedtree_fbx_spm_unit_probe"
VERSION = 1
REQUIRED_GENERATOR_TYPES = {"Frond", "Leaf Mesh"}


class UnitProbeError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise UnitProbeError(f"Unit probe evidence does not exist: {path}")
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _positive(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise UnitProbeError(f"{label} must be numeric.") from exc
    if not math.isfinite(result) or result <= 0.0:
        raise UnitProbeError(f"{label} must be finite and greater than zero.")
    return result


def _identity(value, tolerance=1.0e-9):
    return abs(float(value) - 1.0) <= tolerance


def evaluate_candidate(candidate, target_meters, tolerance_ratio):
    geometry_scale = _positive(
        candidate.get("mesh_geometry_scale"),
        "Candidate FBX geometry scale",
    )
    mesh_asset_scale = _positive(
        candidate.get("mesh_asset_scale"),
        "Candidate SpeedTree Mesh Scale",
    )
    generator_scale = _positive(
        candidate.get("generator_scale", 1.0),
        "Candidate generator scale",
    )
    non_identity = [
        name
        for name, value in (
            ("FBX_GEOMETRY", geometry_scale),
            ("SPM_MESH_ASSET", mesh_asset_scale),
        )
        if not _identity(value)
    ]
    duplicate_scale = len(non_identity) > 1 or not _identity(generator_scale)
    location = (
        non_identity[0]
        if len(non_identity) == 1
        else ("IDENTITY" if not non_identity else "DUPLICATED")
    )

    measurements = candidate.get("response_measurements")
    response_mode = isinstance(measurements, list) and bool(measurements)
    if not response_mode:
        measurements = candidate.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        raise UnitProbeError("Unit probe candidate has no SpeedTree measurements.")
    rows = []
    for measurement in measurements:
        if not isinstance(measurement, dict):
            raise UnitProbeError("Unit probe measurement must be an object.")
        generator_type = str(measurement.get("generator_type") or "")
        if response_mode:
            measured = _positive(
                measurement.get("measured_response"),
                f"{generator_type or 'Generator'} measured response",
            )
            expected = _positive(
                measurement.get("expected_response"),
                f"{generator_type or 'Generator'} expected response",
            )
            relative_error = abs(measured - expected) / expected
        else:
            measured = _positive(
                measurement.get("measured_extent_meters"),
                f"{generator_type or 'Generator'} measured extent",
            )
            expected = target_meters
            relative_error = abs(measured - target_meters) / target_meters
        evidence = measurement.get("evidence")
        if not evidence:
            raise UnitProbeError(
                f"{generator_type or 'Generator'} measurement has no evidence file."
            )
        row = {
            **measurement,
            "generator_type": generator_type,
            "relative_error": relative_error,
            "within_tolerance": relative_error <= tolerance_ratio,
            "evidence": _fingerprint(evidence),
        }
        if response_mode:
            row.update(
                {
                    "measurement_mode": str(
                        measurement.get("measurement_mode")
                        or "actual_speedtree_scale_response"
                    ),
                    "measured_response": measured,
                    "expected_response": expected,
                }
            )
        else:
            row["measured_extent_meters"] = measured
        rows.append(row)
    generator_types = {row["generator_type"] for row in rows}
    common_types_ready = REQUIRED_GENERATOR_TYPES.issubset(generator_types)
    if response_mode:
        physical_import_multiplier = _positive(
            candidate.get("physical_import_multiplier"),
            "Observed Blender-to-SpeedTree import multiplier",
        )
        required_effective_scale = 1.0 / physical_import_multiplier
        physical_relative_error = (
            abs(geometry_scale * mesh_asset_scale - required_effective_scale)
            / required_effective_scale
        )
    else:
        physical_import_multiplier = None
        required_effective_scale = geometry_scale * mesh_asset_scale
        physical_relative_error = 0.0
    verified = bool(
        not duplicate_scale
        and common_types_ready
        and physical_relative_error <= tolerance_ratio
        and all(row["within_tolerance"] for row in rows)
    )
    return {
        "name": str(candidate.get("name") or location),
        "mesh_geometry_scale": geometry_scale,
        "mesh_asset_scale": mesh_asset_scale,
        "generator_scale": generator_scale,
        "scale_location": location,
        "effective_scale": geometry_scale * mesh_asset_scale,
        "duplicate_scale": duplicate_scale,
        "generator_types": sorted(generator_types),
        "measurements": rows,
        "max_relative_error": max(row["relative_error"] for row in rows),
        "physical_import_multiplier": physical_import_multiplier,
        "required_effective_scale": required_effective_scale,
        "physical_relative_error": physical_relative_error,
        "measurement_policy": (
            "actual_speedtree_scale_response"
            if response_mode
            else "absolute_speedtree_extent"
        ),
        "verified": verified,
    }


def select_unit_probe_contract(
    candidates,
    *,
    target_meters=0.1,
    blender_scale_length=1.0,
    tolerance_ratio=0.05,
    selected_candidate_name=None,
    selection_reason=None,
):
    target_meters = _positive(target_meters, "Physical target meters")
    scale_length = _positive(blender_scale_length, "Blender scale_length")
    tolerance_ratio = _positive(tolerance_ratio, "Unit probe tolerance ratio")
    evaluated = [
        evaluate_candidate(candidate, target_meters, tolerance_ratio)
        for candidate in candidates
    ]
    selected_candidate_name = str(selected_candidate_name or "").strip()
    if selected_candidate_name:
        matches = [
            row for row in evaluated if row["name"] == selected_candidate_name
        ]
        if len(matches) != 1:
            raise UnitProbeError(
                "The user-selected SpeedTree unit candidate must resolve exactly "
                f"once: {selected_candidate_name!r}."
            )
        selected = matches[0]
        measured_types = set(selected["generator_types"])
        response_verified = (
            not selected["duplicate_scale"]
            and REQUIRED_GENERATOR_TYPES.issubset(measured_types)
            and all(
                bool(row.get("within_tolerance"))
                for row in selected["measurements"]
            )
        )
        if not response_verified:
            raise UnitProbeError(
                "The user-selected SpeedTree unit candidate did not preserve one "
                "common measured response for Frond and Leaf Mesh."
            )
        selection_authority = "user_art_direction"
        selection_policy = (
            "user_selected_measured_role_independent_scale_location"
        )
        selection_reason = str(selection_reason or "").strip()
        if not selection_reason:
            raise UnitProbeError(
                "A user-selected SpeedTree unit candidate requires a reason."
            )
    else:
        verified = [row for row in evaluated if row["verified"]]
        if not verified:
            raise UnitProbeError(
                "No common role-independent SpeedTree unit candidate matched the "
                f"{target_meters:g}m physical probe."
            )
        verified.sort(
            key=lambda row: (
                row["max_relative_error"],
                row["scale_location"],
                row["name"],
            )
        )
        selected = verified[0]
        selection_authority = "measured_physical_fit"
        selection_policy = (
            "lowest_real_speedtree_response_error_with_one_role_independent_scale_location"
        )
        selection_reason = None
    result_rows = []
    for row in selected["measurements"]:
        result = {
            "generator_type": row["generator_type"],
            "status": "verified",
            "same_unit_contract": True,
            "relative_error": row["relative_error"],
            "evidence": row["evidence"],
            "measurement_mode": row.get("measurement_mode"),
        }
        if "measured_extent_meters" in row:
            result["measured_extent_meters"] = row["measured_extent_meters"]
        else:
            result["measured_response"] = row["measured_response"]
            result["expected_response"] = row["expected_response"]
        result_rows.append(result)
    return {
        "kind": KIND,
        "version": VERSION,
        "status": "verified",
        "physical_target_meters": target_meters,
        "tolerance_ratio": tolerance_ratio,
        "blender_units": {
            "system": "METRIC",
            "scale_length": scale_length,
            "target_blender_units": target_meters / scale_length,
        },
        "selected": {
            "candidate_name": selected["name"],
            "mesh_geometry_scale": selected["mesh_geometry_scale"],
            "mesh_asset_scale": selected["mesh_asset_scale"],
            "generator_scale": 1.0,
            "scale_location": selected["scale_location"],
            "effective_scale": selected["effective_scale"],
        },
        "generator_results": result_rows,
        "candidates": evaluated,
        "selection_authority": selection_authority,
        "selection_policy": selection_policy,
        "selection_reason": selection_reason,
        "physical_scale_match": bool(selected["verified"]),
        "physical_relative_error": selected["physical_relative_error"],
        "required_effective_scale": selected["required_effective_scale"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--measurements", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target-meters", type=float, default=0.1)
    parser.add_argument("--blender-scale-length", type=float, default=1.0)
    parser.add_argument("--tolerance-ratio", type=float, default=0.05)
    parser.add_argument("--select-candidate")
    parser.add_argument("--selection-reason")
    args = parser.parse_args()
    source = Path(args.measurements).expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(candidates, list):
        raise UnitProbeError("Unit probe measurements must contain a candidate list.")
    result = select_unit_probe_contract(
        candidates,
        target_meters=args.target_meters,
        blender_scale_length=args.blender_scale_length,
        tolerance_ratio=args.tolerance_ratio,
        selected_candidate_name=args.select_candidate,
        selection_reason=args.selection_reason,
    )
    target = Path(args.output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("SPEEDTREE_UNIT_PROBE=" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
