"""Decide whether a Send2UE export should consume DynamicWind JSON.

Normalized cluster prototypes are rigid, reusable parts. Their exported
armature intentionally contains only ``part_root``; the final tree Assembly
rebinds those parts to the Full SK skeleton and consumes the Full SK wind
contract there. Treating a prototype as a final skeleton either looks for a
non-existent suffixed JSON or applies the source rig's incompatible JSON.
"""

CLUSTER_GENERATED_FLAG = "speedtree_cluster_generated"
CLUSTER_ASSET_ROLE_KEY = "speedtree_cluster_asset_role"
CLUSTER_ARMATURE_ROLE = "skeletal_armature"
CLUSTER_MESH_ROLE = "skeletal_mesh"
CLUSTER_PART_ROOT = "part_root"

WIND_MODE_FINAL_SKELETON = "final_skeleton"
WIND_MODE_DEFERRED_ASSEMBLY = "deferred_to_final_assembly"
WIND_MODE_DISABLED = "disabled"


def classify_normalized_cluster_prototype(export_objects):
    """Return a serializable classification from Blender object facts."""
    objects = list(export_objects or [])
    armatures = [row for row in objects if row.get("type") == "ARMATURE"]
    meshes = [row for row in objects if row.get("type") == "MESH"]

    normalized = bool(armatures and meshes)
    normalized = normalized and all(
        bool(row.get("cluster_generated"))
        and row.get("asset_role") == CLUSTER_ARMATURE_ROLE
        and list(row.get("bone_names") or []) == [CLUSTER_PART_ROOT]
        for row in armatures
    )
    normalized = normalized and all(
        bool(row.get("cluster_generated"))
        and row.get("asset_role") == CLUSTER_MESH_ROLE
        and list(row.get("vertex_groups") or []) == [CLUSTER_PART_ROOT]
        for row in meshes
    )

    return {
        "normalized_cluster_prototype": bool(normalized),
        "armature_count": len(armatures),
        "mesh_count": len(meshes),
        "armatures": [
            {
                "name": row.get("name"),
                "bone_names": list(row.get("bone_names") or []),
                "cluster_generated": bool(row.get("cluster_generated")),
                "asset_role": row.get("asset_role"),
            }
            for row in armatures
        ],
        "meshes": [
            {
                "name": row.get("name"),
                "vertex_groups": list(row.get("vertex_groups") or []),
                "cluster_generated": bool(row.get("cluster_generated")),
                "asset_role": row.get("asset_role"),
            }
            for row in meshes
        ],
    }


def resolve_dynamic_wind_policy(
    export_objects,
    *,
    explicit_skip=False,
    cluster_assembly_status=None,
):
    """Resolve a fail-closed DynamicWind handoff policy."""
    target = classify_normalized_cluster_prototype(export_objects)

    if explicit_skip:
        mode = WIND_MODE_DISABLED
        reason = "DynamicWind was explicitly disabled for this Push"
    elif cluster_assembly_status == "ready":
        mode = WIND_MODE_FINAL_SKELETON
        reason = "Final Cluster Assembly requires the Full SK wind contract"
    elif target["normalized_cluster_prototype"]:
        mode = WIND_MODE_DEFERRED_ASSEMBLY
        reason = (
            "Normalized one-bone cluster prototype; DynamicWind is applied "
            "after rebinding to the final Assembly skeleton"
        )
    else:
        mode = WIND_MODE_FINAL_SKELETON
        reason = "Export is a final skeletal target and requires DynamicWind JSON"

    return {
        "mode": mode,
        "requires_json": mode == WIND_MODE_FINAL_SKELETON,
        "reason": reason,
        "target": target,
        "cluster_assembly_status": cluster_assembly_status,
    }


__all__ = [
    "CLUSTER_ASSET_ROLE_KEY",
    "CLUSTER_GENERATED_FLAG",
    "WIND_MODE_DEFERRED_ASSEMBLY",
    "WIND_MODE_DISABLED",
    "WIND_MODE_FINAL_SKELETON",
    "classify_normalized_cluster_prototype",
    "resolve_dynamic_wind_policy",
]
