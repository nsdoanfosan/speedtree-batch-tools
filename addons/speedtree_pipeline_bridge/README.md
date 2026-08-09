# SpeedTree Pipeline Bridge

This API-only Blender add-on is the supported boundary between Batch workers
and the installed `speedtree_bone_weight_repair`, `atlas_leaf_mesh_builder`,
and `send2ue` add-ons.

External callers import `speedtree_pipeline_bridge.api`, request named
capabilities with `prepare_runtime()`, persist `session.receipt`, and resolve
only the operations granted to that session. The bridge fails before mutation
when an add-on is missing, its public API is incompatible, a required symbol is
absent, or an explicitly configured source path differs from the module Blender
actually loaded.

The package has no UI panel and stores no Blender scene state.
