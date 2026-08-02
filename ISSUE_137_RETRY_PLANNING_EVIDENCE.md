# Issue #137: failed-retry planning throughput evidence

## Scope and safety

The benchmark fixture contains 154 generated targets: 100 durable current
successes, 24 stale Repair candidates, 10 structured Send2UE failures, 10
Unreal failures sharing one parent manifest/checkpoint, and 10 intentionally
ambiguous fail-closed rows. Checked state alternates, so unchecked inventory
rows remain in scope. Two shared 1 MiB reports force repeated-evidence behavior.

Every file is generated under a temporary directory. The benchmark and tests
do not discover or read production `D:` assets, launch a DCC/BAT, or inspect,
control, or terminate a running application process.

Command:

```powershell
python tools\benchmark_issue137_retry_planning.py --runs 2 --output issue137_optimized.json
```

The output receipt is intentionally local/ephemeral because its generated temp
paths differ per run. The committed fixture descriptor and regression test
enforce the stable counts and semantic digest.

## Measured baseline before the production planner change

| metric | cold | warm |
|---|---:|---:|
| wall time | 1.770623 s | 0.664588 s |
| peak traced memory | 11,865,283 B | 11,842,441 B |
| file reads | 338 | 338 |
| bytes read | 10,554,674 | 10,554,674 |
| JSON parses | 184 | 184 |
| SPM contract parses / fresh Repair decisions | 154 | 154 |
| shared parent manifest reads | 10 | 10 |
| shared parent checkpoint reads | 10 | 10 |

Cold baseline spans were 1.609057 s in Repair-state validation, 0.048554 s in
durable-evidence loading, and 0.075019 s in parent-manifest loading. Thus the
unconditional 154-target fresh Repair pass consumed 90.9% of cold wall time;
the warm run retained exactly the same reads, bytes, and parses.

## Optimized result

| metric | cold | warm |
|---|---:|---:|
| wall time | 0.339851 s | 0.300318 s |
| peak traced memory | 4,801,186 B | 4,758,910 B |
| file reads | 112 | 112 |
| bytes read | 2,108,873 | 2,108,873 |
| JSON parses | 58 | 58 |
| SPM contract parses / fresh Repair decisions | 54 | 54 |
| shared parent manifest reads | 1 | 1 |
| shared parent checkpoint reads | 1 | 1 |

Cold planner spans were 0.006336 s for cheap candidate discovery, 0.242686 s
for fresh Repair validation, 0.028015 s for durable evidence, 0.023470 s for
parent grouping/validation, 0.004730 s for both classification passes, and
0.011776 s for immutable snapshot/commit work.

Compared with baseline, cold wall time fell 80.8%, reads fell 66.9%, bytes read
fell 80.0%, JSON parses fell 68.5%, fresh SPM/Repair decisions fell 64.9%, and
peak traced memory fell 59.5%. The exact observed improvement varies with the
filesystem cache, so the regression gates semantic identity and deterministic
I/O/parse counts rather than a fragile timing threshold.

## Correctness receipt

- Baseline and optimized candidate classifications, partitions, selection
  order, and eligibility metadata have the same canonical SHA-256:
  `1f272cf0a48da46e5e9dc96968e434440c6311472d30fa84f1912125396aed54`.
- The optimized fixture routes 10 targets to immutable Unreal-only recovery,
  then 34 to Blender/Send2UE/Unreal, preserving the established ordering.
- The complete 154-row inventory remains the discovery scope, including every
  unchecked row. Only 100 durable successes with an unchanged saved live-file
  stat identity skip fresh expensive validation; missing/changed identity,
  legacy state, structured failure, cancellation, wait, and ambiguity remain
  fresh fail-closed candidates.
- Durable reports use a bounded LRU keyed by canonical path, stable stat tuple,
  and SHA-256 content identity. The fixture proves two report misses plus eight
  hits. The shared manifest and checkpoint each parse once per identity; parent
  schema validation also runs once.
- Parent dependency closure, source proofs, immutable Unreal-only validation,
  final fixed-point reconciliation, exact repair planning, and job construction
  operate on one state/inventory/config generation. Planner-side state/status
  writes are deferred to the main-thread commit.
- Existing #107/#118/#125 regressions continue to cover terminal disposition,
  full inventory, exact repair plan generation, dependency closure, sibling
  isolation, immutable Unreal-only validation, and partition ordering.
- Planning publishes bounded substage/scanned/cache/last-unit diagnostics plus
  a real progress/heartbeat timestamp and checks cancellation between bounded
  units. Dead-owner interpretation and restore reconciliation remain solely in
  #138 and are not duplicated here.

## Validation

- Focused retry/orchestration/progress/recovery: 142 passed, 6 subtests passed.
- Issue #137 regression file: 6 passed.
- Full SK Batch clean rerun: 754 passed, 61 subtests passed.
- Compile gate: 229 Python sources, 4 contract groups, passed.

Issue #138 owns planning-owner liveness and must land first. This branch is
integrated on merge `7c55ff2` from #139 before publication, without changing
`LIVE_STAGES` or restore reconciliation here.
