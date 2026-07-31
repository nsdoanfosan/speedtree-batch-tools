# SpeedTree Cluster Card Pipeline

This package keeps two contracts separate:

- `SK_<cluster>.spm -> SK_<cluster>.blend`: raw 3D mesh/data export handled by the existing BWR pipeline.
- `<cluster>.spm -> <cluster>.blend`: three camera-projected embedded cutouts handled here.

The card pipeline requires explicit camera SPM, tree SPM, camera name, material name, and mesh IDs. It never derives an origin from bounds and never uses `Straighten`. It preserves the embedded cutout mesh origin, topology, UV float payload, and Cutout pivot/angle metadata, then bakes the SpeedTree Dropped XY camera right/up/normal basis into identity-transform Blender objects.

Example:

```powershell
python -m cluster_card_pipeline.cli `
  --camera-spm 'D:\path\Cluster\branch_elm_01.spm' `
  --tree-spm 'D:\path\SK_Tree_elm_01.spm' `
  --camera-name 'Dropped XY plane camera 2' `
  --material 'M_branch_elm_01' `
  --mesh-ids 1 2 9 `
  --output-prefix 'branch_elm_01' `
  --output-dir 'D:\path\card_pipeline_outputs\branch_elm_01' `
  --blender-exe 'C:\Program Files\Blender Foundation\Blender 5.1\blender.exe' `
  --speedtree-exe 'C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\win64\SpeedTree_Modeler.exe' `
  --speedtree-fbx-options 'D:\path\Options_Fbx.ini' `
  --speedtree-xml-options 'D:\path\Options_HI_Xml.ini'
```

Outputs include:

- `branch_elm_01.blend`
- `meshes/branch_elm_01_01~03.fbx` and `.obj`
- normalization and Blender/FBX round-trip reports
- SpeedTree handoff copies that preserve tree material ID 8, mesh IDs 1/2/9, and existing Generator references
- an Assembly prototype manifest with `SK_branch_elm_01_01~03` public asset names

The Assembly manifest remains `prototype_ready` until real tree instance bindings exist. The current tree material owns 1/2/9, but its active Frond slot explicitly selects mesh 1; the pipeline does not invent placements for 2/9.

## Camera atlas refresh

SpeedTree Modeler 10.1 does not expose Camera Export through its command line. The Blender integration therefore uses `capture_refresh.py` as a fail-closed two-stage contract instead of pretending the existing Substance rerender job refreshes cluster captures:

1. `begin_camera_capture_request()` records the exact camera SPM hash, camera metadata, material, Color/Opacity requirements, dimensions, dependencies, and available optional-map fingerprints.
2. Export Color and Opacity from the same saved camera in SpeedTree. Normal/Gloss/Subsurface/AO/Height maps are optional for this plan/UV contract.
3. `finalize_camera_capture_request()` requires Color and Opacity to have been rewritten after the request and proves that the SPM/dependencies did not change.

`ensure_camera_capture_refresh()` is the public integration point. It accepts a still-valid receipt, finalizes an existing request, or creates a request and stops with an actionable path. Receipts are content-addressed and same-path texture replacements are supported.
