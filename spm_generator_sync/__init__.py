"""Public engine API for the SPM Generator Sync tool and SK Batch integration."""

from .spm_generator_sync import (
    CATEGORY_COLORS,
    SyncError,
    apply_group_transaction,
    build_sync_plan,
    load_manifest,
    scan_tree_folders,
    suggest_base_map,
)

__all__ = [
    "CATEGORY_COLORS",
    "SyncError",
    "apply_group_transaction",
    "build_sync_plan",
    "load_manifest",
    "scan_tree_folders",
    "suggest_base_map",
]
