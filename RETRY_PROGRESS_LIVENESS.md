# Failed retry progress and liveness contract

Issue #107 adds a durable observer layer to the failed Blender/Unreal retry
paths. Issue #138 extends that contract to pre-enqueue planning ownership and
safe full-pipeline fallback when immutable Unreal recovery cannot be proven.

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
ordinal/total, **wall elapsed** time, an evidence state, separate last
progress/output/heartbeat ages, and a bounded latest diagnostic. Wall time is
never execution evidence. Shared-queue wait includes FIFO position and the
current lease owner identity when another owner is ahead.

The receipt's `selection_context` is `historical_failed_or_stale_retry_targets`.
It is intentionally not the current run outcome. While any target is
non-terminal, the panel shows `success N`, `failed N`, `remaining N`, and a
pending terminal outcome. The current state is `running` only with live owner
or heartbeat evidence; it may instead say `stalled`, `owner_lost`, or an
unknown-owner state. If an individual target has failed while another remains
live, it explicitly says the current run continues. Only after every selected
target is terminal does the panel display the terminal outcome. Receipts expose
this distinction as `run_state` (`running`, `waiting`, or `terminal`) and, after
terminalization only, `terminal_outcome`; the root `stage` remains a
current-target observation until then.

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

Planning is owned before any queue job exists. Its receipt records the runtime
owner ID, hostname, PID, process creation marker, planning session ID, thread
identity, heartbeat, ready state, commit claim, and commit completion. A
dedicated non-Tk monitor renews only that exact planner session. A slow planner
with a live heartbeat stays live even when wall time grows; wall time without a
heartbeat cannot do so.

The default `retry_stall_warning_seconds` is **120 seconds**. Crossing it with
a fresh heartbeat changes the visible state to `stalled` and exposes the
already-enabled Stop action. This warning does not terminate anything. New
progress returns the target to its prior live stage.

The default `retry_owner_lost_seconds` is **45 seconds**. A confirmed-absent
exact planning owner or a shared-queue `owner_lost` lease reconciliation
produces the distinct durable `owner_lost` result. An expired planning
heartbeat whose process is still present is `failed`, not falsely healthy.
Already completed targets are kept terminal and are not scheduled again by
receipt reconstruction.

## Full-pipeline fallback eligibility

A structured failed Push history is not discarded merely because its immutable
Unreal parent proof is missing, incomplete, or no longer current. Immutable
Unreal-only recovery still requires current proof; when that proof fails, the
retry planner routes the exact target through a forced Blender -> Send2UE ->
Unreal pipeline instead. If a structurally valid parent manifest already proved
an exact dependency closure, those providers are included in the same fallback
job even when their own Blender outputs are current. This prevents the former
`current provider excluded` + `dependent tree blocked` zero-job dead end.

Existing child phase timeouts remain authoritative where configured. Their
cleanup calls the #100 process lifecycle using the recorded process handle and
private Windows Job Object; a timeout or Stop never searches for or kills
Blender, Unreal, Modeler, MCP, Python, or any other executable by name.

## Durable receipt

Each retry click creates one atomic receipt under:

`%LOCALAPPDATA%\SpeedTreeBatchTools\retry_progress`

The receipt records one row per exact selected target, including partition,
execution path, ordinals, stage timestamps, all three liveness timestamps,
bounded diagnostic, outcome, and terminal reason. It also records the exact
planning owner/session state plus each exact shared queue job ID, sequence,
status, owner identity, and receipt run ID.
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

On startup the UI loads the latest receipt without rendering it, reconciles its
exact job IDs with the shared queue, probes the exact planning PID + creation
marker, terminalizes stale legacy planning receipts without owner identity, and
only then renders the last trustworthy state. Corrupt, unsupported, or
path-escaping latest pointers are ignored rather than guessed.

## Tk and cancellation boundary

Retry classification/planning runs on a background Python thread. It performs
no Tk operation. It posts an immutable plan to `ui_queue`; only
`_drain_ui_queue` on the main thread shows dialogs, changes widgets, or enqueues
the plan. The receipt durably claims `committing` before enqueue, so duplicate
ready events cannot enqueue twice. Cooperative cancellation terminalizes
uncommitted planning rows as `cancelled` and wins before that claim.

Stop cancels still-waiting exact queue tickets. For an active target it sets the
cooperative stop flag; the existing worker poll then closes only the exact
owned process tree through #100 and seals the target as `cancelled` after the
worker returns. Pre-existing/manual external applications are outside that
ownership boundary and survive.
