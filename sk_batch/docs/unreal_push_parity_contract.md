# SK Batch Unreal Push parity contract

## Scope

`rpc` and `headless` are transport choices only. Both consume the same
versioned manifest and execute the same Unreal-side ingest function. A transport
must not reinterpret Send2UE settings or replace the Send2UE importer with a
generic FBX task.

Both transports must also preserve the channel meanings and failure rules in
the [Tree Vertex Color contract](tree_vertex_color_contract.md); transport is
not allowed to remap, regenerate, or discard R/G.

## Verified source contract (2026-07-14)

- Runtime: Blender 5.1.2; custom Send2UE 2.6.7 installed as a junction to
  `C:\Users\PARK\Documents\GitHub\BlenderTools\src\addons\send2ue`.
- Representative asset: `SK_bush_blackgum_01.blend`, template `default.json`.
- Saved Send2UE mode is `send_to_project`; SK Batch forces animation export off.
- The manifest preserves the complete Send2UE `asset_data` and the exact
  `settings.get_extra_property_group_data_as_dictionary(..., only_key="unreal_type")`
  result. This includes FBX normals/tangents, smoothing, vertex-color replacement,
  skeleton/physics/LOD, material order, Nanite, collision, and destination paths.
- The serialized PhysicsAsset settings remain in the manifest for audit parity,
  but SK Batch intentionally overrides PhysicsAsset generation at import time.
  SpeedTree reference meshes do not use ragdoll, simulation, or skeletal
  collision; this exception does not change interactive character Send2UE.
- Extension dispatch order is Send2UE's existing `dir(properties.extensions)`
  order. For the representative SK asset the material pipeline is enabled and
  the other Unreal import modifiers are inactive.

## Ordered item transaction

1. Blender pre-operation and pre-mesh-export extensions run normally.
2. UE Unique Names refreshes/validates the handoff JSON before FBX export.
3. Send2UE exports with `path_mode=send_to_disk`; no Unreal connection is needed.
4. The manifest records the post-extension-mutated `asset_data`, exact Unreal
   `property_data`, sidecar paths, wind JSON, output fingerprints, and the
   serialized Unreal pre/post commands produced by the same extension methods.
5. Unreal checks out existing mesh, Skeleton, PhysicsAsset, material master/layer,
   MI/MYI, and texture packages that the existing pipeline may mutate.
6. Material `preflight_mesh_materials` runs.
7. Send2UE's own `UnrealRemoteCalls.import_asset` imports the FBX with the
   manifest property data while SK Batch temporarily forces
   `create_physics_asset=False`.
8. Material `process_mesh` runs, including texture/MI creation and slot assignment.
9. LOD and socket operations run when present.
10. The imported SkeletalMesh is verified with Nanite enabled and Shape
    Preservation set to Voxelize, while Support Ray Tracing, per-poly collision,
    and its PhysicsAsset assignment are disabled. A default generated
    PhysicsAsset is deleted only when it has no foreign referencer.
11. Dynamic wind is applied through `CodexDynamicWindImportLibrary`.
12. Assets/directories are saved, assigned slots are verified, and relevant
    materials are compiled/checked.

RPC invokes this transaction through the open editor's existing Send2UE RPC
bridge. Headless invokes the identical transaction in one
`UnrealEditor-Cmd.exe -run=pythonscript` session for the pending batch.

## Queue and recovery contract

- `data_error` and `manual_required` are item-local; record and continue.
- Before each Unreal item, atomically write `importing` plus its fingerprint and
  retry count to the checkpoint.
- A commandlet crash leaves that checkpoint as ground truth. The watchdog marks
  the item `unreal_crash`, restarts, and resumes without replaying completed items.
- After the per-item retry ceiling, isolate the item as `manual_required` and
  continue. After the batch restart ceiling, remaining items become `not_run`.
- Stable terminal states are `imported_ok`, `data_error`, `manual_required`, and
  `not_run`; `unreal_crash` remains visible while retry/recovery is pending or
  when the watchdog ceiling is reached. Exported but not yet ingested is
  `exported_pending_unreal`.
- Every state entry carries its manifest/report/log/checkpoint paths.

## Cache contract

The export fingerprint covers the `.blend` content, exporter/manifest schema and
relevant add-on sources. The import fingerprint additionally covers serialized
asset/property data, hook commands, sidecar/wind data, and exported file hashes.
Cached work is reusable only when the manifest and all referenced files still
match; `--force` bypasses both caches.

## GUI compatibility

- The existing `③ Unreal Push` button remains and defaults to `rpc`.
- A transport selector permits explicit `rpc` or `headless` runs.
- The full unattended pipeline defaults to `headless`, preserving the existing
  one-click workflow while removing its open-editor requirement.

## Runtime parity evidence (2026-07-14)

- MyProject2: Unreal Engine 5.8.0, `UnrealEditor-Cmd -run=pythonscript`.
- Headless and open-editor RPC both completed `SK_bush_blackgum_01` through the
  same manifest runner with `imported_ok`.
- Both audits matched exactly: SkeletalMesh class, one LOD, Skeleton path, empty
  PhysicsAsset, two slot names and MI paths, bounds origin, and bounds extent.
- Both dynamic-wind results matched: 99 joints, five simulation groups, five
  simulation-group bones, 16 bone chains, 99 extra bones, and hash
  `12009740390698720721`.
- Both material reports matched and compiled
  `/Game/Material/Tree/AssetTree/Master/M_TreeAsset_Master`.
- Verification artifacts are listed in
  `logs/verification_headless_rpc_parity.json`.
