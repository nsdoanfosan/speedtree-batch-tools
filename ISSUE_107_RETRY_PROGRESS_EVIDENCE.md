# Issue #107 before/after retry receipt

All runtime evidence below uses only the sanitized Python helper in
`sk_batch/tests/retry_progress_helper.py`. No production retry was launched, no
production application was stopped, and no `D:` asset was read or modified by
the acceptance helper.

## Before

The issue report's durable shared-queue evidence ended at the job level:

```json
{
  "status": "failed",
  "failure_reason": "owner_lost",
  "result": null
}
```

The retry button then showed only a partition summary such as "Blender/
Send2UE→Unreal · 34 items". There was no exact current target, target ordinal,
queue owner/position, stage, liveness age, bounded diagnostic, or per-target
terminal result from which the UI could reconstruct completed work.

## After

One retry click creates one receipt with disjoint target rows and exact queue
job identities. A sanitized mixed owner-loss result has this shape:

```json
{
  "kind": "failed_retry_progress_receipt",
  "schema_version": 1,
  "stage": "owner_lost",
  "terminal_reason": "owner_lost",
  "queue_jobs": {
    "unreal_ingest": {
      "job_id": "exact-sanitized-job-id",
      "sequence": 11,
      "status": "failed",
      "owner": {
        "owner_id": "sk_batch:sanitized-host:run-id",
        "pid": 505,
        "heartbeat_at": 1000.0
      }
    }
  },
  "targets": [
    {
      "target_name": "completed.spm",
      "partition": "unreal_ingest",
      "partition_ordinal": 1,
      "partition_total": 2,
      "stage": "complete",
      "outcome": "complete",
      "terminal_reason": "completed"
    },
    {
      "target_name": "owner_lost.spm",
      "partition": "unreal_ingest",
      "partition_ordinal": 2,
      "partition_total": 2,
      "stage": "owner_lost",
      "outcome": "owner_lost",
      "terminal_reason": "owner_lost"
    }
  ]
}
```

The first row stays complete when the exact queue lease is reconciled as
owner-lost. Reopening SK Batch loads `latest.json`, reopens that exact receipt,
reconciles only its recorded queue job IDs, and never schedules either row.

## Production read-only stale-running receipt

The following observation was recorded without changing the queue job,
terminating a process, or reading/modifying any `D:` asset. It anchors the UI
contract in the reported reproduction and does not treat an operator close as
the original failure cause.

| Time (KST) | Read-only observation |
| --- | --- |
| 14:41–14:43 | Lauraceae, ladyfern, and Nothofagus material preflights each recorded `status=ok`; a shared contract mismatch had not blocked startup. |
| while the UI said “failure” | Shared-queue sequence 81 (`실패/stale 재시도 · Blender/Send2UE→Unreal · 83개`) was still `status=running`, with `result=null`. This is an active retry, not its terminal outcome. |
| app close | The operator closed SK Batch after interpreting the visible failure state as a whole-job failure. |
| 14:43:00–14:43:30 | The owner PID 23260 was absent; its last heartbeat was 14:43:00, lease expiry 14:43:30, and queue update 14:43:01. |
| 15:11 | The queue eventually recorded `failed`, `failure_reason=owner_lost`, and `result=null`. |

The durable receipt therefore records `operator_app_close` before a later
`owner_lost_reconciled` event when that sequence is observed. The latter is a
post-close owner-loss outcome, not an assertion that the owner loss caused the
earlier material/preflight state.

## Sanitized acceptance matrix

| Scenario | Observable receipt/UI result | Process result |
| --- | --- | --- |
| Slow child | live stage; progress/output/heartbeat ages refresh | normal exit |
| Silent healthy child | live stage; output age increases; heartbeat remains fresh | normal exit, never false-complete |
| Hung child | `stalled` after the documented warning threshold | remains alive until operator cancel |
| Operator cancel | `cancelled` with exact target/partition reason | #100 private owned Job tree terminated |
| Durable success kinds | `completed` for `completed` / `imported_ok` / `ready`; excluded from failure totals and tokens | no replay |
| Export waiting | `pending_unreal`, `run_state=waiting`; excluded from failure totals and owner-lost liveness | no false failure |
| Late Stop after 29/29 completion | queue/result remains `completed`, failed/blocked `0` | no completed row is rewritten cancelled |
| Active operator Stop | target state stores `*_status_kind=cancelled` plus `*_status_result`; no `*_status_error=internal_error` | owned lease seals as queue `cancelled` |
| Unrelated helper | absent from retry receipt | remains alive across owned-tree cancel |
| Non-zero child | `failed`, `process_nonzero_exit:7` | exact return code retained |
| Queue owner lost | `owner_lost` only for incomplete rows | completed rows preserved |
| Restart | exact completed/blocked/failed/cancelled/owner-lost rows restored | no duplicate scheduling |
| Historical retry failure plus active row | current state `running`; success/failure/remaining counters; terminal outcome remains pending | individual failure does not stop or relabel the current batch |
| Operator closes app, owner later disappears | ordered `operator_app_close` then `owner_lost_reconciled` receipt events | no causal claim is made about the earlier retry state |
| State commit/receipt crash boundary | target state saves before receipt `complete`; an exact completed queue result later reconciles the missing receipt transition | no automatic replay or receipt-only asset trust |

## Verification receipt

- Latest terminal semantics regressions: `7 passed` (sequence 82 success/wait
  filtering, sequence 83 late Stop, durable operator cancel, active-lease
  cancellation, and restart reconstruction).
- Full push/queue flow: `93 passed`; retry progress: `10 passed`; shared queue
  suites: `45 passed`.
- Full SK Batch unittest discovery: `642 passed`.
- Full repository-root unittest discovery: `261 passed`.
- Compile gate: `210 Python sources`, `4 contract groups`, revision
  `ea6a392bd143d64c`.
- `compileall` and `git diff --check`: passing.
- The helper cancellation check asserts the unrelated sanitized Python process
  remains alive while the exact owned hung root/grandchild Job tree is clean.
