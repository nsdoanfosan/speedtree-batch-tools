# Process lifecycle contract

## BAT ownership policy

All four BAT launchers make an explicit durable handoff. `start` guarantees
only that Windows created `launch_guard.pyw`; it is not a readiness handshake.
The guard creates the lifecycle supervisor before importing GUI code, so no
worker can start before the ownership boundary exists. Closing the BAT console
after process creation does not pretend to cancel the visible GUI.
Closing/stopping the GUI, or terminating the supervisor, closes the exact Job
handles that own its workers.

Each BAT sets `SPEEDTREE_BATCH_LAUNCH_SOURCE` before the handoff. The value is
persisted in the run receipt, so a BAT-launched run cannot be confused with a
direct Python launch.

## Ownership boundary

| Category | Launch paths | Shutdown behavior |
| --- | --- | --- |
| `tool_owned` | Generator Sync streamed Modeler exports; SK Blender/Python/Modeler workers; PCG Blender, SBS cooker/render, target refresh, and Unreal commandlets; Cluster source/relationship/card jobs; short `tasklist` observations; token migration helper | Created suspended, assigned to the session Job and a private tree Job, then resumed. Stop targets the private Job. Supervisor loss closes the session Job. |
| `manual_modeler_handoff` | stale Node-table recovery Modeler opened for an operator Save | Recorded with PID + process creation identity but never assigned to a Job and never terminated by this tool. |
| `standalone_gui_handoff` | integrated UI's "Open standalone" action | Shell handoff is recorded without claiming ownership. The new BAT/guard creates its own lifecycle session. |
| `shell_handoff` | Explorer/folder-open actions | Recorded as intentionally surviving; never assigned or terminated. |
| external/pre-existing | an already-open Unreal Editor used through RPC, existing Blender/Modeler/Python/MCP processes, and unrelated `cmd.exe` processes | Observed only when needed. They are never discovered for termination and are never assigned to a tool Job. |

`process_lifecycle.py` is the only production module allowed to call Windows
Job APIs or raw `subprocess.Popen`. Production call sites use `owned_run`,
`owned_popen`, `run_streaming_process`, or one of the explicit external handoff
functions. There is no executable-name or PID-only kill path.

## Windows launch and shutdown sequence

1. Require Windows 8 / Server 2012 or newer, then create unnamed,
   non-inheritable session and per-launch Jobs with `KILL_ON_JOB_CLOSE` and no
   UI limits. Breakaway creation flags are rejected.
2. Create the child with `CREATE_SUSPENDED`.
3. Assign its retained process handle to the session Job first and private Job
   second. Every use and close of a Job handle shares the same per-Job lock.
4. Record run ID, launch ID, PID, process creation identity, source, and command
   digest in the receipt.
5. Resume the exact process handle.
6. On Stop/close, allow the application's already-issued cancellation request
   a bounded cooperative observation window. Windows `Popen.terminate()` is
   not called or labeled graceful because it is `TerminateProcess`.
7. If the tree remains, terminate its private Job and wait the bounded forced
   cleanup period.
8. On GUI/supervisor loss, Windows closes the last session/private Job handles
   and applies kill-on-close only to their members. The next launch seals the
   interrupted receipt after confirming the exact owner PID + creation
   identity is absent, but does not claim it observed descendant exit.

PID values in receipts are evidence, not termination targets. Runtime cleanup
uses retained process and Job handles, so an exit/reuse race cannot redirect a
signal to a new process.

## Shutdown receipts

Receipts default to
`%LOCALAPPDATA%\SpeedTreeBatchTools\process_receipts\<run-id>.json`. Tests and
diagnostics can set `SPEEDTREE_PROCESS_RECEIPT_DIR` to an isolated directory.
Each receipt contains:

- run ID, BAT/direct launch source, owner PID + process creation identity;
- every owned launch ID, PID + creation identity, source, command digest and
  bounded argv evidence;
- graceful and forced cleanup result per owned tree;
- external/manual handoffs kept outside the Job boundary;
- final survivor observation and the basis for forced-owner-exit recovery.

Receipt writes are atomic. A receipt that remains `running` because the owner
was forcibly terminated is recovered only after the exact owner identity is
confirmed absent. Access denial or any other uncertain identity query fails
closed and leaves the receipt running. A confirmed receipt is then sealed as
`recovered_forced_owner_exit` with the
Job-close guarantee. Because the dead owner cannot query its former Job, the
recovered receipt sets `survivors` to `null` and
`survivor_observation=unavailable_after_owner_exit`; sanitized acceptance
evidence separately proves the recorded child identities exited.

## Sanitized acceptance evidence

`tests/process_lifecycle_helper.py` creates only test Python parent/grandchild
trees. `tests/test_process_lifecycle.py` covers normal close, cooperative and
forced Stop, root crash with a silent descendant, forced supervisor
termination, BAT process-creation handoff, nested Jobs, two concurrent owned
roots, launch/shutdown races, handle inheritance, breakaway rejection,
repeated cycles, PID exit/reuse races, fail-closed receipt recovery, and an
external sibling that must survive. No production Blender, Unreal, Modeler,
MCP, Python, `cmd.exe`, or D: asset is started, stopped, or modified by these
tests.
