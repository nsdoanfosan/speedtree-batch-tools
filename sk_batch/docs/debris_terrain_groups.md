# Independently selected terrain group prefabs

`jobs/debris_terrain_group_job.py` supports source-generic adaptive prefab mode and the earlier reviewed-evidence mode. It preserves complete connected leaf pieces, original world geometry, UVs, colors, materials and corner normals. The canonical Blender source retains its intact original `Export` collection; optionally saved parent Empties and mesh children stay in the separate `DebrisTerrainGroups` collection.

Activation comes only from the selected SPM row's independent **지형 그룹** action in the BAT table. It has no filename, Wind preset, Wind-enabled state or category filter, and there is no global activation checkbox. Original Wind metadata is audited and preserved. Generated groups use **`static_rest_pose`**: they retain the source's verified rest-pose geometry, but do not retain its original wind motion. Every group or legacy control Push requires explicit `--skip-wind` for these derivative StaticMesh exports; it does not change the original source's Wind settings. These exports are independent StaticMesh assets and do not use skeletal or nested Blueprint assembly import.

## Ordinary Push integration and scope

Each row's activation is persisted by canonical SPM path, defaults to inactive, and is captured in the queued job configuration. Changing the table later affects future requests, rather than an already queued job. An inactive row skips new prefab generation/update and leaves its existing DataAsset binding intact.

After the existing original Push has successfully completed its Unreal import, the optional pipeline uses three stages:

1. Build adaptive whole-part groups in a disposable Blender process through the public BWR operation, then export through the existing Send to Unreal worker. The canonical source is not saved.
2. Read imported native `SOURCE_MODEL` geometry and require the independent bidirectional position-cloud check at 0.001cm, exact group coverage and conserved triangle counts before finalizing a verified payload.
3. Author/configure a Blueprint with the actual N group components, then change only `PrefabClass` on existing DataAsset rows that match the exact typed source asset. Preserve the original fields, weights, array ordering and unmatched rows; do not add selection rows. A Blueprint without a matching existing row remains reusable without inventing a DataAsset entry.

The GUI completion hook also covers successful cache/recovery/retry and delayed Unreal-import completion. Failed or pending original imports do not start this stage. Its receipts and failures are separate from the original Push report. Exact Push opts in with `--debris-terrain-prefab`; a deferred export carries that request into the later import-completion step and does not claim the prefab is complete at export time. The pipeline entry is `sk_batch.debris_prefab_pipeline.run_pipeline`.

Generic asset authoring is independent of category. **Production `PCG_01` currently applies the terrain-group placement node only after the Debris `Loop_46` branch.** Other Weed/Tree spawn branches are not connected to this stage. Preserving or assigning an existing row's `PrefabClass` in those categories is metadata for future reuse, not verified terrain-group placement there. Extending those branches remains future work after the separate cost investigation.

On 2026-10-01, the pine01 service completed with `status='ok'` and 16 groups, unchanged source bytes, preserved original fields across 47 DataAssets, unchanged source Wind, and restored editor authoring guards. Its receipt is `C:/UnrealProjects/MyProject2/Saved/WeedDebrisGroupingAnalysis/debris_prefab/ec6cce5adfef5611/pipeline.json`. That invocation supplied the exact existing SkeletalMesh and recorded `original_push_report=null`; it proves the service stages, rather than a newly executed table-button-to-original-Push sequence. Build and asset publication evidence is tracked separately in the project worklog.

## Worker arguments

| Argument | Behavior |
| --- | --- |
| `--group-evidence PATH` | Reviewed grouping evidence, including exact source signatures and whole-part ownership. |
| `--asset-suffix 10` | Exact source suffix in the evidence. |
| `--group-report PATH` | Current group/helper/runtime receipt; separate from the existing Push report. |
| `--asset-base-name NAME` | Optional stable generated asset base; otherwise the source helper chooses it. |
| `--save-groups` | Save generated non-Export groups to the same canonical `.blend`, after verified Perforce recovery and checkout. Omit on export retry. |
| `--groups-only` | Build and validate groups; stop before export isolation or existing Push. |
| `--control-only` | Validate the selected source partition, then export one intact source-derived control mesh in memory. Rejects `--save-groups` and `--groups-only`. |
| `--control-asset-name NAME` | Required with `--control-only`; exact new StaticMesh name, for example `SM_weed_deadleaves_10_Terrain_Control`. |
| `--reuse-existing-materials` | Full Push disables Send2UE material-pipeline creation/update and material/texture import in this process. Existing Unreal materials are assigned separately to the new meshes. |

Other arguments pass unchanged to the existing `send2ue_push_job.py`. Wrapper arguments never reach that parser. Full Push negotiates all required BWR material/group, Send2UE export/RPC and unique-name JSON capabilities before creating any group geometry. A groups-only run requests only the group provider; the optional reuse flag additionally validates Send2UE fields.

Control mode additionally requests `debris_terrain_control_v1` and resolves `activate_terrain_control_export` before group creation. Ordinary group mode does not require that capability. Control activation must report `status='activated'` and `group_count=1`; ordinary activation must match the validated group count.

## Generic adaptive prefab mode

Use `--source-path CANONICAL.blend` to select `debris_terrain_prefab_v1` and derive whole connected parts directly from the currently open original Export mesh. No leaf asset name, numeric suffix, existing point-template registry or precomputed labels are required. The currently verified source contract is Skeletal geometry with exact native vertex identity, one non-root Start owner per whole connected part, evaluated/raw geometry parity and preserved attributes. Sources with other topology or ownership contracts must pass those checks; selecting a row does not bypass them. Saved Wind metadata is provenance, not an eligibility gate.

`--max-groups` defaults to **20 as a work budget**, not a forced count; its safety range is 1..256. Candidates use deterministic XY area-weighted Lloyd Voronoi assignment and minimum enclosing footprint-circle pivots. Choose the smallest actual count satisfying `--target-radius-cm` (default 30) and `--target-height-span-cm` (default 10); otherwise report the best budget-limited candidate with `budget_insufficient=true`. Height spread is the original whole-part minimum-Z proxy. Neither threshold guarantees contact with arbitrary terrain.

A full generic group export also requires `--source-skeletal-mesh /Game/.../SK_Asset` for native source identity, geometry provenance and exact DataAsset matching. It imposes no NONE/disabled requirement and does not modify that source's Wind metadata. Wrapper-owned optional arguments are `--prefab-asset-path /Game/.../BP_Asset`, `--prefab-manifest PATH`, and `--prefab-materials-json PATH` (a JSON list of exact existing material package paths). The worker binds its actual N groups to the actual Send2UE exported parent/child identities and package paths, then writes a versioned Blueprint payload. This export worker itself does not create or modify a Blueprint or DataAsset; the optional pipeline performs those verified downstream stages. A generic groups-only run needs no Unreal path and produces only the build/evidence receipts.

Integration evidence on 2026-10-01 selected 18 groups for source09 and 20 for source10 within the default work budget. Both actual Send2UE exports and strict native SOURCE_MODEL readback passed: source09 has 125,601 vertices and 157,898 triangles; source10 has 356,633 vertices and 443,431 triangles. Both source file hashes remain unchanged. Native Blueprints were saved with the actual 18/20 components, and the two existing DA_Base_06_SK rows received their respective PrefabClass references. The schema/assignment audit preserved the original fields across 47 DataAssets and 758 rows.

Bounded managed Generate/Regenerate/Cleanup trials passed for both prefabs: one source09 anchor produced 18 group instances, and two source10 anchors produced 40. The existing fallback path also generated and cleaned up correctly. A production Cliff_final_01_SK Generate produced 120,824 managed group instances from 3,598 source09 and 2,803 source10 clusters, with 31 original-mesh fallback clusters. The group stage took approximately 26.5 seconds, requested 119 rendered tiles and logged 628,272 initial collision trace calls; complete regeneration took approximately 99 seconds. The rendered-height helper additionally validates its contact inputs with collision queries. These are one generation's observations, not an FPS benchmark or a general performance guarantee.

The payload supplies actual-N authored component names, relative UE transforms, five component-local floor probes per group, source hashes and provenance. Generic `ContactPolicyVersion=2` uses each group's lowest source underside as its contact floor while retaining the original component transforms and within-group leaf arrangement. Explicit `--prefab-floor-policy source_global` retains the earlier global-floor policy1 for comparison. In adaptive source10, per-group floors remove up to 4.47cm of additional group elevation; its within-group underside height spans of 8.3..12.45cm remain. This is not a claim of every leaf contacting terrain within 2cm.

The DA remains the selection source: one row references the prefab class. Native Blueprint creation/configuration and import geometry verification are downstream steps; export payloads explicitly set `import_geometry_verified=false`. `finalize_import_verified_manifest` requires the exact expected-header bytes, their receipt hash, unchanged source SHA-256, exact group assets/pivots, native bidirectional cloud PASS and conserved triangle counts at≤0.001cm before producing a newly hashed verified payload. The native actor reads meshes and relative transforms from its authored Blueprint components. The earlier 09/10 twenty-group experiment below remains historical evidence, not a runtime count rule.

When finalizing material overrides, also provide the hash-bound actual native-header bytes and an explicit mapping of canonical source slot names to existing material package paths. The helper follows each imported group's ordered native slots; a group using only the second source material gets that material, rather than a truncated first entry. Missing or ambiguous names fail. Default/WorldGrid material paths cannot be accepted as production material bindings.

For a source-preserving generic export, use the existing Push arguments with this worker-owned prefix (the source must already be open in the disposable background process):

```text
--source-path CANONICAL.blend --max-groups 20 --group-report GROUP_RECEIPT.json
--source-skeletal-mesh /Game/Folder/SK_Source --prefab-manifest PREFAB_PAYLOAD.json
--reuse-existing-materials --transport headless_export --skip-wind
```

Append the existing required `--report`, `--spm`, `--material-contract` and current export/manifest/ingest paths. `headless_export` is the existing export-only transport; there is no `--export-only` switch. Omit `--save-groups` to preserve the source bytes. A retry may use fresh explicit `--checkpoint`, `--batch-report` and `--item-import-report` paths while reusing the stable FBX directory.

## Repeat the generic workflow for another selected source

Use a stable source-specific export directory and receipt names. The source path, exact SkeletalMesh, existing material interfaces, target Blueprint and existing matching DataAsset rows are explicit inputs; none is inferred from a particular leaf number or Wind preset. Reuse the installed optional PrefabClass schema. Another compatible source in the existing Debris route does not require a graph edit or registry entry. Other spawn branches are not currently wired to the terrain-group stage, even when their source and prefab assets pass authoring checks.

1. Verify the canonical source/SPM identity, current material wrapper and Perforce recovery point. Run the adaptive build in a separate background Blender process. Review actual N, thresholds and `budget_insufficient`; a budget-limited result may need a larger budget or smaller, more local groups.
2. Export through the existing Send2UE worker with `headless_export`, explicit `--skip-wind`, material reuse and no `--save-groups`. Export success leaves native import and Blueprint verification pending.
3. Collect the matching expected Blender group cloud and native imported SOURCE_MODEL cloud. Require exact asset coverage, conserved triangle counts and bidirectional positions within 0.001cm. Resolve material overrides by actual imported slot names.
4. Finalize a new verified payload, author its Blueprint through the native preview/fingerprint/apply helper, and connect its generated class only to existing exact source matches. The automated pipeline can update matching rows across DataAssets; the manual single-row adapter below requires exactly one match per call. Save only the reviewed Blueprint, group meshes and matching DataAssets.
5. Run the bounded managed lifecycle trial. Production Generate is a separate explicit action, and currently uses the installed Debris branch only. Retain final receipts and recovery dependencies; remove disposable scripts and intermediate dumps once their evidence is preserved.

For example, fill these inputs for the selected source and use the same values on retries:

```powershell
$terrainBlender = 'C:/Program Files/Blender Foundation/Blender 5.2/blender.exe'
$terrainRepo = 'C:/Users/PARK/Documents/GitHub/speedtree-batch-tools'
$terrainBlend = '<canonical source .blend>'
$terrainSpm = '<canonical source .spm>'
$terrainContract = '<current source-specific material wrapper .json>'
$terrainSkeletalMesh = '/Game/<folder>/SK_<source>'
$terrainBlueprint = '/Game/<folder>/BP_<source>_TerrainPrefab'
$terrainKey = '<stable source queue key>'
$terrainOutput = '<stable receipt directory>'
$terrainExport = '<stable source-specific FBX directory>'
$terrainSend2UE = 'C:/Users/PARK/Documents/CodexWorktrees/send2ue-guide-routing-20260916/src/addons/send2ue/dependencies/unreal.py'
& $terrainBlender --background --factory-startup $terrainBlend --python-exit-code 7 --python "$terrainRepo/sk_batch/jobs/debris_terrain_group_job.py" -- --source-path $terrainBlend --max-groups 20 --target-radius-cm 30 --target-height-span-cm 10 --group-report "$terrainOutput/group_build.json" --source-skeletal-mesh $terrainSkeletalMesh --prefab-asset-path $terrainBlueprint --prefab-manifest "$terrainOutput/prefab_export.json" --reuse-existing-materials --report "$terrainOutput/push.json" --spm $terrainSpm --material-contract $terrainContract --transport headless_export --skip-wind --queue-id $terrainKey --export-root $terrainExport --manifest "$terrainOutput/export_manifest.json" --checkpoint "$terrainOutput/checkpoint.json" --batch-report "$terrainOutput/ingest.json" --item-import-report "$terrainOutput/item_import.json" --unreal-ingest "$terrainRepo/sk_batch/unreal_ingest.py" --send2ue-unreal-py $terrainSend2UE
```

The project helpers live under `C:/UnrealProjects/MyProject2/Scripts/PCGTests/`. `dump_debris_group_expected.py` reads original Export geometry and generated groups in the current Blender process. For an unsaved source, invoke it after a deterministic `--groups-only` rebuild with exactly the exported source, budget, thresholds and optional asset base name. Pass `--manifest export_manifest.json` and `--output-dir EXPECTED_DIR`; it checks actual Send2UE origin/axis/import settings and binds the expected header to that manifest. This rebuild and dump do not export or save the source. Do not reopen the unchanged source and assume unsaved groups will be present.

Execute the following native stages in the live Unreal editor through the existing remote-execution runner. Load project scripts by their absolute file paths with `importlib.util`; importing them does not start a trial or author assets. These calls mutate the exact declared import/Blueprint/DA targets and require the corresponding checked-out/recoverable asset scope.

```python
# Loaded BAT sk_batch/unreal_ingest.py module, inside Unreal Python.
ingest.run_manifest(export_manifest_path, checkpoint_path, ingest_report_path)

# Read-only input adapter: the export Blueprint payload is not a geometry-dump manifest.
dump_manifest = {"groups": [{"group_id": g["group_id"],
                             "asset_path": g["mesh_asset_path"]}
                            for g in export_payload["groups"]]}
Path(dump_manifest_path).write_text(json.dumps(dump_manifest), encoding="utf-8")
geometry.dump_imported_geometry(dump_manifest_path, native_geometry_output_dir)
```

`geometry` is `debris_terrain_group_geometry.py`. Use fresh bounded readback output paths, then preserve the final proof rather than accumulating copies. Do not append an old deployed whole-cluster mesh as `original_asset_path` unless it is proven to match the canonical source; a same-source control is optional and separately verified.

Run the strict verifier outside the editor:

```powershell
& '<Python with numpy>' 'C:/UnrealProjects/MyProject2/Scripts/PCGTests/verify_debris_group_import.py' --expected-header '<EXPECTED_DIR>/expected_group_geometry.json' --actual-header '<NATIVE_DIR>/import_geometry.json' --tolerance-cm 0.001 --output '<geometry_comparison.json>'
```

Use the pure BAT finalizer only after that receipt passes. `canonical_materials_by_slot_name` is an explicit mapping from original source slot names to existing production material package paths; actual imported slot order is authoritative for each group.

```python
from sk_batch.debris_prefab_manifest import finalize_import_verified_manifest
verified = finalize_import_verified_manifest(
    export_payload, geometry_receipt,
    expected_geometry_header_bytes=Path(expected_header_path).read_bytes(),
    actual_geometry_header_bytes=Path(native_header_path).read_bytes(),
    material_bindings=canonical_materials_by_slot_name)
Path(verified_payload_path).write_text(
    json.dumps(verified, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
```

Do not manually flip `import_geometry_verified`. The finalizer checks source/hash/asset/pivot/geometry/material bindings and recalculates the payload hash. Native Blueprint authoring rejects an unverified payload.

Inside Unreal, load `author_debris_terrain_prefab.py` as `author`, then use its source-generic entry points:

```python
authored = author.author_verified_prefab(verified_payload_path, blueprint_report_path, save=True)
assigned = author.assign_prefab(dataasset_path, source_row_asset_path,
                                authored["generated_class"], category="Debris")
assert assigned["original_fields_preserved"]
assert unreal.EditorAssetLibrary.save_loaded_asset(unreal.load_asset(dataasset_path),
                                                   only_if_is_dirty=True)
```

`source_row_asset_path` is the existing row's exact StaticMesh or SkeletalMesh; `assign_prefab` requires exactly one match and changes only PrefabClass. It does not save the DataAsset itself. `ensure_compatible_schema` and `debris_prefab_graph_integration.py` provide the one-time schema/graph migration, with baseline/fingerprint checks; they are already installed in this project. The native actor and Blueprint components are the group mesh/transform source of truth.

For a bounded functional check, load `debris_prefab_managed_trial.py` and call `start_managed_trial(prefab_class_path, fallback_mesh_path, anchors, report_path, dataasset_path=dataasset_path, material_slots=maximum_verified_slot_count)`. Each anchor supplies translation, quaternion and scale. The asynchronous receipt must finish successfully through repeated Generate and Cleanup before production regeneration. Actual N comes from the Blueprint; this helper does not author or save the production graph.

## Source preservation

The worker checks original Export mesh topology, semantic attributes, transforms, parenting, material names and collection membership before/after grouping. Optional save uses `save_version=0` temporarily and restores the previous setting in `finally`; user preferences are never saved.

After that optional save, the worker changes only the disposable process: `combine_assets.combine='child_meshes'`, `use_object_origin=True`, both static import/LOD `remove_degenerates=False`, and optional material reuse flags. Each active RNA field is checked before group construction. Triangle preservation is generic: very small, nonzero source triangles must pass the unchanged import triangle-count gate. BWR activates only the generated groups in Export without deleting the original source data. Existing `bpy.ops.wm.send2ue` produces the FBX files and manifest. Send2UE sets each asset's `empty_object_name` to its owning parent; the active exporter subtracts that Empty's world translation, so each generated asset is local to its reviewed group pivot.

Use a current material handoff **wrapper** validated against the exact canonical SPM and content-addressed SPM/STMAT envelope. A historical material preflight report without `canonical_spm` and `material_source_spm` wrapper fields is not a Push wrapper. The existing `sk_batch/logs/SK_weed_deadleaves_10_push_material_contract_current.json` satisfies this source's wrapper contract; verify current source contents before reuse.

## Legacy 09/10 reviewed-evidence example

This section preserves the earlier 09/10 fixed-evidence experiment and its source10 paths. It is legacy comparison evidence, not the current generic activation policy or a fixed runtime group count. Those sources happened to be disabled-NONE; that fact is not an eligibility rule for independently selected SPM rows. Run Blender in a separate background process, leaving the user's GUI session unchanged.

```powershell
$terrainBlender = 'C:/Program Files/Blender Foundation/Blender 5.2/blender.exe'
$terrainRepo = 'C:/Users/PARK/Documents/GitHub/speedtree-batch-tools'
$terrainBlend = 'D:/OneDrive/Forestportfolio/02_nature/Tree/weed_deadleaves/SK_weed_deadleaves_10.blend'
$terrainEvidence = 'C:/UnrealProjects/MyProject2/Docs/PCG/verification/weed_debris_voronoi20_20261001.json'
$terrainOutput = 'C:/UnrealProjects/MyProject2/Saved/WeedDebrisGroupingAnalysis'
& $terrainBlender --background --factory-startup $terrainBlend --python-exit-code 7 --python "$terrainRepo/sk_batch/jobs/debris_terrain_group_job.py" -- --group-evidence $terrainEvidence --asset-suffix 10 --group-report "$terrainOutput/group_build.json" --groups-only
```

After authorized original-file save, full export can reuse the saved source with `--save-groups` omitted:

```powershell
& $terrainBlender --background --factory-startup $terrainBlend --python-exit-code 7 --python "$terrainRepo/sk_batch/jobs/debris_terrain_group_job.py" -- --group-evidence $terrainEvidence --asset-suffix 10 --group-report "$terrainOutput/group_export_build.json" --reuse-existing-materials --report "$terrainOutput/group_push.json" --spm 'D:/OneDrive/Forestportfolio/02_nature/Tree/weed_deadleaves/SK_weed_deadleaves_10.spm' --material-contract "$terrainRepo/sk_batch/logs/SK_weed_deadleaves_10_push_material_contract_current.json" --transport headless_export --skip-wind --queue-id weed_deadleaves_10_terrain20 --export-root 'D:/SpeedTreeBatchTools/send2ue_fbx/headless/debris_terrain_10' --manifest "$terrainOutput/group_manifest.json" --unreal-ingest "$terrainRepo/sk_batch/unreal_ingest.py" --send2ue-unreal-py 'C:/Users/PARK/Documents/CodexWorktrees/send2ue-guide-routing-20260916/src/addons/send2ue/dependencies/unreal.py'
```

`exported_pending_unreal` means actual Send2UE export succeeded; Unreal ingest remains pending. Invoke existing `unreal_ingest.run_manifest` in the live editor through its native remote-execution connection, using the manifest's checkpoint and report paths. The manifest freezes export and ingest-code fingerprints. Re-export after changing ingest code rather than bypassing artifact verification. A failed terminal checkpoint item is skipped for the same fingerprint; a newly exported fingerprint permits retry.

## Geometry gate and legacy 09/10 source-derived control

Before comparing terrain placement, prove that the imported actual-N group union contains exactly the current Blender source geometry. Validate each asset's parent-pivot-local positions, triangle count and reconstructed source-world union, with the actual Send2UE axis/unit/import settings. A count or bounds check alone is insufficient; imported vertices may be split or reordered, so use a bidirectional position-cloud comparison within the declared tolerance.

If the previously deployed whole-cluster mesh differs from that source, preserve the mismatch evidence and export a new one-mesh control from the same canonical source. This avoids treating an old asset's extra geometry as a grouping result. The legacy source10 experiment required this control because the deployed StaticMesh contained additional geometry; both its then-20-group union and source-derived Control passed their geometry checks. The control arguments below belong to that reviewed-evidence comparison path, not the ordinary generic pipeline's activation mechanism.

For Control, keep the same reviewed evidence, SPM, current material wrapper, reuse flags and existing Push arguments. Add `--control-only --control-asset-name SM_weed_deadleaves_10_Terrain_Control`, omit `--save-groups`, and use distinct report, manifest, export-folder and queue-id paths. The helper clones the intact original source into one Empty/mesh export unit with pivot zero, preserves its world geometry and attributes, and removes the cloned Armature modifier/vertex groups. It never saves that control into the source `.blend`. Verify the imported Control against the intact original source cloud before using it as the placement baseline.

## Static import behavior

The shared BAT ingest routes static-only units directly to the existing Send2UE importer, bypassing Skeleton refresh, physics, skeletal optimization, DynamicWind, Assembly construction and runtime probes. It requires no wind JSON and `requires_json=False`. Existing static assets can be reimported without reading a Skeleton property. After import, the actual asset must be a StaticMesh before material audit.

Static audit uses `StaticMesh.static_materials` and records passive pre/post inventories for every imported static asset. Null, empty and DefaultMaterial slots explicitly report `needs_assignment`; import success alone does not prove the intended production material assignment. The result also reports `material_assignment_pending`. Static audit does not change shared master usage flags, recompile/save materials, or run skeletal section audits.

UE 5.8's unattended FBX reimport reads the existing mesh's `FbxStaticMeshImportData`, even when the task requests `replace_existing_settings=True`. Static-only ingest therefore synchronizes the explicitly declared Boolean `remove_degenerates` to that exact checked-out target before calling the unchanged Send2UE importer, and verifies the imported value afterward. Other settings and skeletal/mixed routes remain unchanged. The receipt records each target's previous, requested and actual setting. This preserves tiny nonzero source triangles during reimport; geometry counts and bidirectional checks remain mandatory.

For a static-only manifest without actionable Cluster Assembly assets, checkout is limited to its declared static mesh paths, and saves cover exact imported packages. It skips the terminal directory save and skeletal thumbnail-free saver. Existing skeletal/mixed import behavior is preserved. Assign the original verified material interfaces to the new group meshes, verify non-null slot identities, and save only those new assets before PCG trial.

Validation includes the existing Unreal ingest tests and static regression cases for protected-property access, passive null/default slot reporting, no shared-master mutation, and exact twenty-package saves with an unrelated dirty material preserved.
