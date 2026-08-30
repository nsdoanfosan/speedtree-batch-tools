# First-run performance and memory policy

This policy concerns only work executed in the current run. Receipt reuse,
artifact caches, and second-run speedups are outside its scope.

## Chosen execution shape

Production uses stage batching with bounded workers:

1. complete the Cluster Blender/Normalizer dependency wave;
2. complete the root Blender Assembly wave;
3. export every eligible Send2UE item with bounded Blender workers;
4. ingest the combined lazy manifest serially in Unreal, checkpointing and
   collecting each item, and recycle the commandlet after six completed items.

The GUI implements the first two barriers in
`_run_full_pipeline_stages()` and `_run_batch_impl()`. The push stage implements
the export batch in `_run_headless_push_batch()`. `unreal_ingest.run_manifest()`
owns item-local checkpoints, compiler drains, GC, and the six-item process
lifetime. Cluster dependencies are still completed before their consumers, so
stage batching does not weaken the native branch/bone contract.

Available physical RAM now caps Assembly and Send2UE worker counts before a
wave begins. The user's configured worker count remains an upper bound. The
conservative envelopes are 6 GiB per Assembly worker, 4 GiB per export worker,
and an 8 GiB system reserve. This prevents paging from turning nominal
parallelism into worse first-run wall time.

## Structure comparison

| Property | Asset-serial `A→B→U` | Stage-batched `all A→all B→all U` |
|---|---:|---:|
| Blender throughput | one runnable asset | two workers by default, RAM-capped |
| Unreal launches for 27 items | 27 | 5 at capacity 6 |
| Unreal peak lifetime | one item/process | one item is collected immediately; process lifetime is at most 6 items |
| GPU/VRAM | commandlet policy dependent | explicit `-NullRHI`; no commandlet D3D device |
| Failure isolation | process per asset | item-local stage result + Unreal item checkpoint; crash resumes the exact item |
| Dependency correctness | naturally local | explicit Cluster/provider wave barrier before consumers |

Asset-serial has the lowest possible concurrent RAM, but pays Blender and
Unreal startup at every asset and leaves cores idle across independent work.
Unbounded stage batching has the best theoretical throughput but an unsafe
peak. The implemented hybrid keeps the throughput benefit for CPU-heavy
Blender stages, uses a single Unreal build at a time, and creates a hard
process-lifetime boundary every six Unreal items.

## First-run memory changes

- Combined 75.35 MiB/27-item manifests are schema-v2 indexes. Each schema-v1
  item payload is loaded only when its dependency-ordered turn begins, checked
  by size and SHA-256, and released before Unreal GC.
- `FinishAssemblyBuild` is an ownership boundary. Native builder, part asset,
  binding, influence, and bone lookup wrappers are cleared immediately on both
  success and failure.
- Material usage normalization and any required compilation run immediately
  after FBX import, before optimization, Nanite base creation, or final
  Assembly. Post-build material work is read-only slot/section/usage audit.
- Each Unreal item disables overlapping asynchronous skinned-asset builds,
  drains compilers before restoring the editor setting, releases Python
  references, and requests immediate commandlet GC.
- Windows Job Object receipts now record exact-tree user/kernel CPU time and
  peak process/job memory for future production measurements.
- The durable process receipt is written before a suspended child is resumed
  and again at its terminal transition. The former immediate post-resume
  rewrite duplicated the full cumulative session JSON without improving crash
  recovery, so it was removed. This saves exactly one atomic receipt replace
  per child launch (18 writes in the asset-serial synthetic run and 14 in the
  stage-batched run) while retaining pre-resume ownership durability.

## Small benchmark

Run from the repository root:

```powershell
python .\tools\benchmark_sk_stage_batching.py --items 6 --workers 2 --unreal-capacity 3
```

The 2026-08-30 local run used identical uncached synthetic CPU/RAM work and did
not open or regenerate production assets:

| Metric | Asset-serial | Stage-batched |
|---|---:|---:|
| Wall time | 5.058 s | 2.621 s |
| Process launches | 18 | 14 |
| Summed user CPU | 2.141 s | 2.125 s |
| Summed kernel CPU | 2.125 s | 1.438 s |
| Measured aggregate peak job memory | 27.3 MiB | 46.5 MiB |

Observed speedup was 1.93× and wall time fell by 2.437 s. The deliberate cost
is a bounded 1.70× synthetic RAM peak from two concurrent Blender workers. The
production RAM cap prevents that trade from crossing the available-memory
envelope. Peak GPU memory is not inferred from Job Objects; Unreal commandlets
avoid it structurally through `-NullRHI`.

This synthetic benchmark validates orchestration overhead and bounded
concurrency, not production asset build duration. Production reports should
use the newly recorded Job Object resource fields and existing Assembly stage
timings to replace the conservative worker envelopes when enough first-run
samples exist.
