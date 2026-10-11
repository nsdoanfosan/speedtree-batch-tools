"""Normalized cluster/leaf UV3 and Assembly delivery policy across species.

This is a semantic policy, independent of the producer's source-code hashes.
Branch prototypes and base materials retain their existing path.
"""

from __future__ import annotations

from copy import deepcopy
import re


POLICY_VERSION = 2
ASPECT_POLICY = "normalized_bbox_y_over_max_xz/sqrt_gain/v2"
NORMALIZED_UV_IMPORT_CONTRACT = "SpeedTreeNormalizedPartUv3/20261010/v2"
NORMALIZED_BEND_ROLE_CODES = {"cluster": 1, "leaf": 1, "leaf_side": 2}
PRODUCTION_OWNER = "ElmAssemblyPartBendProduction20261009/v1"
UV_IMPORT_CONTRACT = "SpeedTreeNormalizedPartUv3/20261009/v1"
UV_WRITER = "NativeMeshDescriptionElmProductionUv3/20261009/v1"
ELM_ROOT = "/Game/Meshes/02_nature/Tree/Tree_elm"
ELM_ASSEMBLIES = tuple(
    f"{ELM_ROOT}/Assembly/SK_Tree_elm_{index:02d}_NaniteAssembly"
    for index in (1, 2, 3)
)
ELM_PROTOTYPES = (
    f"{ELM_ROOT}/Cluster/SK_leaf_elm_01_01",
    f"{ELM_ROOT}/Cluster/SK_leaf_elm_side_01_01",
    f"{ELM_ROOT}/Cluster/SK_leaf_elm_side_01_02",
    f"{ELM_ROOT}/Cluster/SK_leaf_elm_side_01_03",
)
SOURCE_MATERIALS = tuple(
    "/Game/Material/Tree/AssetTree/MI/" + name
    for name in ("MI_bark_common_end_01", "MI_Bark_elm_01", "MI_leaf_elm_atlas_01")
)
VARIANT_MATERIALS = tuple(
    "/Game/Material/Tree/AssetTree/AssemblyBend/" + name + "_AssemblyBend"
    for name in ("MI_bark_common_end_01", "MI_Bark_elm_01", "MI_leaf_elm_atlas_01")
)
DEFAULT_SCALARS = {
    "AssemblyBranchWeightExponent": 2.0,
    "SideStiffness": 0.75,
    "AssemblyBranchSpeed": 0.025,
    "AssemblyBranchFlexibility": 0.1,
    "AssemblyBranchTurbulence": 0.8,
    "AssemblyBranchIndependence": 0.75,
    "AssemblyBranchBendCm": 10.0,
    "AssemblyBranchOscillationCm": 25.0,
}


def package_path(value):
    return str(value or "").split(".", 1)[0].rstrip("/")


def elm_leaf_role(asset_name):
    """Canonical token matching: Elm is a species, not a filename substring."""
    name = str(asset_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = name.split(".", 1)[0]
    tokens = re.sub(r"^sk_", "", name.casefold()).split("_")
    if not tokens or tokens[0] != "leaf":
        return None
    family = [token for token in tokens[1:] if token != "side"]
    if not family or family[0] != "elm":
        return None
    return "leaf_side" if "side" in tokens[1:] else "leaf"


def normalization_part_bend_policy(asset_name, role="AUTO"):
    """Explicit authoring metadata wins; names are a legacy AUTO fallback."""
    requested = str(role or "AUTO").strip().lower()
    if requested == "auto":
        role = normalized_part_role(asset_name)
    elif requested in {"none", "branch"}:
        role = None
    elif requested in NORMALIZED_BEND_ROLE_CODES:
        role = requested
    else:
        raise ValueError("Unsupported normalized part bend role: " + str(role))
    if role is None:
        return None
    return {
        "schema_version": POLICY_VERSION,
        "role": role,
        "role_code": NORMALIZED_BEND_ROLE_CODES[role],
        "uv_name": "nanite_part_bend",
        "uv_index": 3,
        "blender_encoding": "U=8+clamp(Y/maxY,0,1)*sqrt(max(1,bboxY/max(bboxX,bboxZ)));V=1-role_code",
        "unreal_encoding": "U=8+mask*aspect_correction;V=role_code",
        "aspect_policy": ASPECT_POLICY,
        "reference_weight_exponent": 2.0,
        "growth_axis_blender": "+Y",
        "growth_axis_unreal": "-Y",
        "anchor_local": [0.0, 0.0, 0.0],
        "protected_uv_indices": [0, 1, 2],
        "vertex_colors_preserved": True,
    }


def normalized_part_role(asset_name):
    """Recognize canonical provider parts without enabling branch prototypes."""
    name = str(asset_name or "").replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0]
    tokens = re.sub(r"^sk_", "", name.casefold()).split("_")
    family = [token for token in tokens[1:] if token != "side"]
    if not tokens or tokens[0] not in {"cluster", "leaf"} or not family or not family[0]:
        return None
    if tokens[0] == "cluster":
        return "cluster"
    return "leaf_side" if "side" in tokens[1:] else "leaf"


def normalized_leaf_role(asset_name):
    """Retain the public leaf-only resolver for existing callers."""
    role = normalized_part_role(asset_name)
    return role if role in {"leaf", "leaf_side"} else None


def part_bend_build_matches_policy(build, policy):
    """A legacy 'current' receipt cannot bless missing normalized UV3."""
    if not policy:
        return True
    if not isinstance(build, dict) or build.get("part_bend_role") != policy["role"]:
        return False
    prototypes = build.get("prototypes")
    if not isinstance(prototypes, list) or not prototypes:
        return False
    if build.get("prototype_count") != len(prototypes):
        return False
    for prototype in prototypes:
        payload = prototype.get("part_bend_payload") if isinstance(prototype, dict) else None
        if not isinstance(payload, dict):
            return False
        if any(type(payload.get(key)) is not int for key in ("schema_version", "role_code", "uv_index")):
            return False
        for key in ("schema_version", "role", "role_code", "uv_name", "uv_index",
                    "blender_encoding", "unreal_encoding", "growth_axis_blender",
                    "growth_axis_unreal", "anchor_local", "aspect_policy",
                    "reference_weight_exponent"):
            if payload.get(key) != policy[key]:
                return False
        hashes = payload.get("protected_uv_sha256")
        if not isinstance(hashes, dict) or set(hashes) != {"0", "1", "2"}:
            return False
        if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
               for value in hashes.values()):
            return False
        if not all(isinstance(payload.get(key), int) and not isinstance(payload[key], bool)
                   and payload[key] > 0 for key in ("vertex_count", "loop_count")):
            return False
        if not valid_aspect_payload(payload):
            return False
    return True


def valid_aspect_payload(payload):
    """Verify measured bounds and gain, so a stale cache cannot bless new UVs."""
    import math
    try:
        bounds = payload["normalized_bounds"]
        lo, hi, size = (bounds[key] for key in ("minimum", "maximum", "size"))
        if any(len(row) != 3 for row in (lo, hi, size)):
            return False
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
               for row in (lo, hi, size) for x in row):
            return False
        if any(x < 0 or not math.isclose(x, b - a, rel_tol=1e-6, abs_tol=1e-9)
               for a, b, x in zip(lo, hi, size)) or hi[1] <= 0:
            return False
        width = max(size[0], size[2])
        if width <= 0:
            return False
        ratio = size[1] / width
        expected = {"aspect_ratio": ratio, "aspect_gain": max(1.0, ratio),
                    "aspect_correction": math.sqrt(max(1.0, ratio))}
        if expected["aspect_correction"] > 3.402823466e38 ** 0.25:
            return False
        return all(not isinstance(payload.get(k), bool) and
                   isinstance(payload.get(k), (float, int)) and
                   math.isfinite(payload[k]) and
                   math.isclose(payload[k], v, rel_tol=1e-6, abs_tol=1e-9)
                   for k, v in expected.items())
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return False


def production_assembly_bend_policy(assembly_path):
    path = package_path(assembly_path)
    if path not in ELM_ASSEMBLIES:
        return None
    return {
        "schema_version": 1,
        "species": "elm",
        "assembly": path,
        "prototypes": list(ELM_PROTOTYPES),
        "roles": [1, 2, 2, 2],
        "source_materials": list(SOURCE_MATERIALS),
        "variant_materials": list(VARIANT_MATERIALS),
        "default_scalars": deepcopy(DEFAULT_SCALARS),
        "preserved_base_slots": [0, 1, 2],
        "preserved_branch_parts": [0, 1, 2],
        "variant_slots": [3, 4, 5],
        "native_import_contract": UV_IMPORT_CONTRACT,
        "native_production_owner": PRODUCTION_OWNER,
    }
