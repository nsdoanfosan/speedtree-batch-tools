# Assembly reimport material defaults

Every Assembly item built through SK Batch ingest applies part-only NoTess
materials after wind/material normalization and before saving the final root.
This includes parts whose wind role is `NONE`. No additional Push option is
needed. Static-only imports and ordinary Full/Base meshes keep their material
route.

Stem materials follow their actual Stem master hierarchy, including reed.
They do not fall back to a bark master. The reed regression covers Stem01,
StemEnd and Stem05 part slots while preserving the separate Base Stem02 slot.

The root retains its original slots. Part remaps use appended aliases copied
from their exact source slot. Geometry, UV3, vertex colors, prototypes, nodes,
skeletons and Base tessellation/displacement are preserved. Repeated ingest
reuses the same material destinations and recognizes existing part aliases.

`sk_batch/assembly_part_notess.py` clones the full material-instance hierarchy
under `/Game/Material/Tree/AssetTree/AssemblyNoTess`. The master and effective
interface must both disable tessellation. Other graph outputs, parameters,
static switches, layers and WPO bounds remain source-equivalent. Existing
production derivatives are reused only after graph/parameter/parent/runtime
verification. An incompatible or edited derivative stops the operation rather
than silently replacing it or publishing a TessON Assembly.

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
