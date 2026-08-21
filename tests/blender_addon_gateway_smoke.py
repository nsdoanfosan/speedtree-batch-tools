"""Blender 5.1 smoke test for the junction-installed integration gateway."""

import json
import sys
from pathlib import Path

import addon_utils


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


bridge = addon_utils.enable(
    "speedtree_pipeline_bridge",
    default_set=False,
    persistent=False,
)
if bridge is None or not addon_utils.check("speedtree_pipeline_bridge")[1]:
    raise AssertionError("speedtree_pipeline_bridge did not enable")

from speedtree_pipeline_bridge.api import prepare_runtime


runtime = prepare_runtime(
    "tests.blender_addon_gateway_smoke",
    {
        "speedtree_bone_weight_repair": (
            "speedtree_export_v1",
            "assembly_pipeline_v1",
            "material_handoff_v1",
            "atlas_manifest_consumer_v1",
        ),
        "atlas_leaf_mesh_builder": (
            "scene_generation_v1",
            "target_registry_v1",
            "source_index_v1",
            "speedtree_publish_v1",
            "atomic_target_transaction_v1",
        ),
        "send2ue": (
            "headless_export_v1",
            "unreal_rpc_v1",
            "fbx_export_v1",
        ),
        "speedtree_cluster_normalizer": ("cluster_normalization_v1",),
        "ue_unique_export_names_addon": ("unreal_handoff_json_v1",),
    },
)

assert runtime.receipt["status"] == "ready", runtime.receipt
assert len(runtime.receipt["addons"]) == 5, runtime.receipt
assert runtime.operation("send2ue", "send_to_disk_path_mode")
assert callable(
    runtime.operation(
        "speedtree_bone_weight_repair", "run_import_and_assemble"
    )
)
assert callable(
    runtime.operation(
        "atlas_leaf_mesh_builder", "execute_external_target_transaction"
    )
)
assert callable(
    runtime.operation(
        "ue_unique_export_names_addon", "refresh_handoff_json"
    )
)

print(
    "BLENDER_ADDON_GATEWAY_SMOKE_OK "
    + json.dumps(
        {
            row["id"]: {
                "source_root": row["source_root"],
                "addon_version": row["addon_version"],
                "mode": row["mode"],
            }
            for row in runtime.receipt["addons"]
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
