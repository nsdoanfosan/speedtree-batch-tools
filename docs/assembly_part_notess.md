# Assembly reimport material defaults

Every Assembly item built through SK Batch ingest applies part-only NoTess
materials after wind/material normalization and before saving the final root.
This includes parts whose wind role is `NONE`. No additional Push option is
needed. Static-only imports and ordinary Full/Base meshes keep their material
route.

Stem materials follow their actual Stem master hierarchy, including reed.
They do not fall back to a bark master. The reed regression covers Stem01,
StemEnd and Stem05 part slots while preserving the separate Base Stem02 slot.

The root retains its original Base slots. Part remaps use appended aliases copied
from their exact source slot. Prototype material slots also receive dedicated
NoTess interfaces; their names, UV density overrides, geometry, UV3, vertex colors,
nodes and skeletons are preserved. Base tessellation/displacement is preserved.
Repeated ingest resolves canonical prototype interfaces back to their exact
source for import matching, then reapplies the same NoTess destinations.

`sk_batch/assembly_part_notess.py` clones the full material-instance hierarchy
under `/Game/Material/Tree/AssetTree/AssemblyNoTess`. The master and effective
interface names end in `_notess`, including already-disabled foliage and Stem.
The master and effective interface must both disable tessellation. Other graph outputs, parameters,
static switches, layers and WPO bounds remain source-equivalent. Existing
production derivatives are reused only after graph/parameter/parent/runtime
verification. An incompatible or edited derivative stops the operation rather
than silently replacing it or publishing a TessON Assembly.

Canonical destinations use the source's full asset name plus `_notess`.
An original name already ending in `_NoTess` gets `_Assembly_notess` so its
independent derivative cannot share the original's case-insensitive short name.
Ownership and the exact original source path are recorded in metadata. UE package
references use case-insensitive identity and `load_asset` restores actual spelling;
case-only references do not create duplicates or fail validation. Graph and
parameter contents retain exact comparisons. Distinct sources requesting the same
canonical destination stop in read-only preflight with both source/parent paths,
the expected destination and existing ownership metadata. No automatic hash name,
fallback name, short-name lookup, merge or overwrite is allowed.
Legacy `_PartNoTess_<hash>` aliases remain readable for existing reimports.

Ingest admits exact derivative packages to Perforce, saves them without
thumbnails, records their durable-save ownership, and only then saves the root.
Already-adopted Willow NoTess parents and artist wind overrides are retained
when their original source stamp and non-wind appearance remain verified.

The module participates in Push dependency hashes, prepared-manifest code
fingerprints, and RPC module invalidation. A cached successful Push is not
evidence that a newly changed delivery module ran.

Regression coverage includes part-only remaps, no-wind parts, shared-slot
deduplication, inheritance/appearance/WPO preservation, repeat ingest, edited
derivative rejection, authored UV-density preservation, dirty-input guards,
and preservation of the existing Willow wind tuning. A separate live
read-only audit checks the existing production derivatives and aliases;
it does not claim a full FBX/Blender/Unreal reimport was performed.
