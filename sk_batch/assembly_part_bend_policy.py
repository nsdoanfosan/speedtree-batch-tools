"""Elm-only normalized leaf UV3 and Assembly material delivery policy.

This is a semantic policy, independent of the producer's source-code hashes.
Branch prototypes, base materials and other species retain their existing path.
"""

from __future__ import annotations

from copy import deepcopy
import re


POLICY_VERSION = 1
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


def normalization_part_bend_policy(asset_name):
    role = elm_leaf_role(asset_name)
    if role is None:
        return None
    return {
        "schema_version": POLICY_VERSION,
        "species": "elm",
        "role": role,
        "role_code": 2 if role == "leaf_side" else 1,
        "uv_name": "nanite_part_bend",
        "uv_index": 3,
        "blender_encoding": "U=8+clamp(Y/maxY,0,1);V=1-role_code",
        "unreal_encoding": "U=8+mask;V=role_code",
        "growth_axis_blender": "+Y",
        "growth_axis_unreal": "-Y",
        "anchor_local": [0.0, 0.0, 0.0],
        "protected_uv_indices": [0, 1, 2],
        "vertex_colors_preserved": True,
    }


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
        for key in ("schema_version", "role", "role_code", "uv_name", "uv_index",
                    "blender_encoding", "unreal_encoding", "growth_axis_blender",
                    "growth_axis_unreal", "anchor_local"):
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
    return True


def production_assembly_bend_policy(assembly_path):
    path = package_path(assembly_path)
    if path not in ELM_ASSEMBLIES:
        return None
    return {
        "schema_version": POLICY_VERSION,
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
