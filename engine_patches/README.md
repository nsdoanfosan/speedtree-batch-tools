# Unreal Engine NaniteBuilder patches

## UE 5.8.1 Nanite Assembly voxel reuse

`ue_5.8.1_nanite_assembly_voxel_reuse.patch` fixes the redundant full-scene
triangle tracing performed when a Voxelize Nanite Assembly is built from parts
that already have voxel hierarchies.

The patch:

- voxelizes the Assembly base once before composition;
- removes only the temporary standalone base root wrapper before the final DAG
  is assembled;
- regrids existing child voxel bricks and copies their material and vertex
  attributes instead of ray tracing every source triangle again;
- keeps direct base references separate from instanced part references until
  both have non-instanced parents, preventing duplicate cluster pages and
  forward/circular streaming dependencies;
- changes the NaniteBuilder derived-data version so patched results cannot be
  confused with stock cached data.

Apply from the Unreal Engine root:

```powershell
git apply C:\path\to\speedtree-batch-tools\engine_patches\ue_5.8.1_nanite_assembly_voxel_reuse.patch
```

The patch is pinned to UE 5.8.1 (`5.8.1-56057345`). Rebuild the
`NaniteBuilder` editor module after applying it.

### Willow verification

The verification used
`SK_tree_Weeping_Willow_01_NaniteAssembly` with 6 registered parts and 2,630
instances. The test changed Preserve Area to Voxelize in memory and did not
save the asset.

| Metric | Stock Voxelize log | Voxel reuse |
| --- | ---: | ---: |
| NaniteBuild | 168.02 s | 11.26 s |
| Built Skeletal Mesh | 176.42 s | 18.49 s |
| Final reduction | 163.07 s | 0.34 s |
| Output clusters | 21,865 | 21,492 |
| Output triangles | 2,661,056 | 2,612,008 |
| Output vertices | 2,662,912 | 2,639,538 |
| Streaming pages | 427 | 415 |
| Total GPU bytes | 54,647,704 | 53,090,384 |
| Total disk bytes | 56,448,449 | 55,955,350 |

The test asset remained byte-identical after the run (SHA-256
`B5A74577923DE10B863610D73969439C46F7F5CC4C5BBCBC4DFD4AC9FF40AF10`).
