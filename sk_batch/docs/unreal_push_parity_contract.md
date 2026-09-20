# SK Batch Unreal Push parity contract

## Scope

`rpc` and `headless` execute the same transaction for generated Nanite
SkeletalMesh, DynamicWind provider, and final Assembly workloads. `unreal_wait`
uses the headless export contract and deliberately stops before Unreal starts.
All modes consume the same versioned item manifest, and a mode must not
reinterpret Send2UE settings or replace the Send2UE importer with a generic FBX
task. Headless adds process isolation and `-NullRHI`; RPC keeps the requested
live-editor workflow while using the shared serial compiler/GC safety controls.

All three modes must also preserve the channel meanings and failure rules in
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
12. Generated SkeletalMeshes are saved through the thumbnail-free package API;
    Skeleton and auxiliary assets use the normal save path. Assigned slots are
    verified and relevant materials are compiled/checked.

## Optional texture availability

Texture completeness is not a transaction precondition. Material identity,
slot identity, mesh payload, and a valid Unreal material interface remain
structural requirements, but an empty or partial texture list is valid input.
Only proven live candidates are serialized or imported. Missing candidates are
left unassigned; ambiguous, stale, or unsafe candidates are omitted. The
material instance is still reused or created and assigned to its mesh slot, and
texture availability never changes the target outcome.

Headless invokes this transaction in one
`UnrealEditor-Cmd.exe -run=pythonscript -NullRHI` session for the pending batch.
The RPC bridge invokes the same runner one item at a time in the open editor.
For both transports, each item temporarily disables overlapping asynchronous
skinned-asset compilation, drains compilers before restoring the editor setting,
releases transient references, performs Unreal GC, and uses thumbnail-free saves
for generated SkeletalMeshes. The provider/part and final-Assembly waves retain
one explicit barrier.

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

- The existing `③ Unreal Push` button remains and defaults to `headless`.
- A transport selector permits `rpc`, `headless`, or `unreal_wait`. An explicit
  RPC selection remains RPC and requires MyProject2 Unreal Editor to be open.
- Saved RPC preferences remain RPC; loading or saving configuration never
  silently rewrites the selected transport.
- `unreal_wait` persists dependency-ordered immutable exports as
  `exported_pending_unreal`. The state and waiting manifest survive GUI restarts.
- `대기 에셋 임포트` revalidates source and export fingerprints, refuses to
  start while MyProject2 Unreal Editor is open, and imports all valid waiting
  rows in one `UnrealEditor-Cmd` session.
- The full unattended pipeline defaults to `headless`, preserving the existing
  one-click workflow while removing its open-editor requirement.

## Historical runtime parity evidence (2026-07-14)

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

This evidence supports using either transport. Production RPC additionally
inherits the current serial compiler-drain, item-GC, thumbnail-free save, and
two-wave Assembly safety controls.
