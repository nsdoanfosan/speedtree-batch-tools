# Failed retry progress and liveness contract

Issue #107 adds an observer layer to the existing failed Blender/Unreal retry
paths. It does not change #79/#89 classification, dependency closure, immutable
Unreal recovery evidence, or fail-closed eligibility.

## Operator stages

The durable stage keys are:

- `planning`
- `shared_queue_wait`
- `claimed`
- `blender`
- `send2ue`
- `unreal`
- `post_check`
- `pending_unreal`
- `stalled`
- `blocked`
- `failed`
- `owner_lost`
- `cancelled`
- `complete`

The SK Batch window shows the exact current target, completed/total, partition
ordinal/total, elapsed time, last progress/output/heartbeat age, and a bounded
latest diagnostic. Shared-queue wait includes FIFO position and the current
lease owner identity when another owner is ahead.

The receipt's `selection_context` is `historical_failed_or_stale_retry_targets`.
It is intentionally not the current run outcome. While any target is
non-terminal, the panel says `current state: running`, shows `success N`,
`failed N`, and `remaining N`, and labels the terminal outcome as pending. If
an individual target has failed while another remains live, it explicitly says
the current run continues. Only after every selected target is terminal does
the panel display the terminal outcome. Receipts expose this distinction as
`run_state` (`running`, `waiting`, or `terminal`) and, after terminalization only,
`terminal_outcome`; the root `stage` remains a current-target observation
until then.

`pending_unreal` is intentionally nonterminal and non-live: export is durable,
but Unreal has not yet supplied the authoritative import result. A receipt whose
remaining rows are in this state has `run_state=waiting`; it is not converted
to `failed`, `stalled`, or `owner_lost` merely because no process heartbeat is
expected while waiting.

## Authoritative target result semantics

Final queue receipts and UI totals normalize durable target kinds as follows:

- `completed`, `imported_ok`, and `ready` -> `completed`
- `exported_pending_unreal` (and an active import checkpoint) -> `pending_unreal`
- `cancelled` and `stopped` -> `cancelled`
- actual blocked, failed, and `owner_lost` outcomes retain separate classes

Success, waiting, and operator-cancelled rows never contribute to final failure
counts, reason-token lists, or failed queue-job lists. A late Stop observation
cannot override an already-authoritative all-target-completed summary. An active
owned lease stopped by the operator is sealed as queue `status=cancelled`, not
`failed`; its structured result retains `outcome=stopped` for compatibility.

## Liveness meanings

The three age clocks intentionally mean different things:

- **progress** changes only at a stage/checkpoint/progress-marker advance.
- **output** changes only when a new child diagnostic line is observed.
- **heartbeat** changes while the exact queue owner or exact owned process is
  still observable.

A silent child can therefore remain `blender`, `send2ue`, or `unreal` with a
fresh heartbeat and an increasing output age. It is never treated as complete.

The default `retry_stall_warning_seconds` is **120 seconds**. Crossing it with
a fresh heartbeat changes the visible state to `stalled` and exposes the
already-enabled Stop action. This warning does not terminate anything. New
progress returns the target to its prior live stage.

The default `retry_owner_lost_seconds` is **45 seconds**. An expired exact
owner heartbeat or a shared-queue `owner_lost` lease reconciliation produces
the distinct durable `owner_lost` result. Already completed targets are kept
terminal and are not scheduled again by receipt reconstruction.

Existing child phase timeouts remain authoritative where configured. Their
cleanup calls the #100 process lifecycle using the recorded process handle and
private Windows Job Object; a timeout or Stop never searches for or kills
Blender, Unreal, Modeler, MCP, Python, or any other executable by name.

## Durable receipt

Each retry click creates one atomic receipt under:

`%LOCALAPPDATA%\SpeedTreeBatchTools\retry_progress`

The receipt records one row per exact selected target, including partition,
execution path, ordinals, stage timestamps, all three liveness timestamps,
bounded diagnostic, outcome, and terminal reason. It also records each exact
shared queue job ID, sequence, status, owner identity, and receipt run ID.
`latest.json` points only to a receipt in that same directory.

It also retains a bounded chronological `lifecycle_events` list. This records
queue registration/claim, an explicit `operator_app_close` observation, and a
later `owner_lost_reconciled` observation when applicable. The order preserves
what was observed without guessing that an operator close was the prior
failure's root cause.

The production target state is written before the receipt can transition that
target to `complete`. A crash after target-state commit but before that receipt
write is reconciled only from the exact shared-queue result; receipt loading
never schedules work. A completed receipt also never bypasses the existing
#79/#89 provenance and fingerprint checks when an operator later starts a new
retry plan.

On startup the UI loads the latest receipt, reconciles its exact job IDs with
the shared queue, and renders the last trustworthy state. Corrupt, unsupported,
or path-escaping latest pointers are ignored rather than guessed.

## Tk and cancellation boundary

Retry classification/planning runs on a background Python thread. It performs
no Tk operation. It posts an immutable plan to `ui_queue`; only
`_drain_ui_queue` on the main thread shows dialogs, changes widgets, or enqueues
the plan.

Stop cancels still-waiting exact queue tickets. For an active target it sets the
cooperative stop flag; the existing worker poll then closes only the exact
owned process tree through #100 and seals the target as `cancelled` after the
worker returns. Pre-existing/manual external applications are outside that
ownership boundary and survive.
