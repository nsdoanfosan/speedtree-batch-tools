# Issue 100 process-tree evidence

All runtime evidence below was produced on Windows with
`tests/process_lifecycle_helper.py`. The helper launches only sanitized copies
of the current Python interpreter. No production Blender, Unreal, Modeler,
MCP, Python, `cmd.exe`, or `D:` asset was inspected, started, stopped, or
modified.

## Before (`origin/main` 0e85e461c2f64a5917b1e95cac08a7828f9ca87b)

Static audit of the baseline found:

- all four BAT files used `start ... pythonw`, so their console process returned
  immediately and owned no descendants;
- 63 direct `subprocess.run`, `subprocess.Popen`, or `os.startfile` source lines;
- Job Object calls split between `sk_batch/sk_common.py` and
  `spm_generator_sync/process_stream.py`, rather than a single ownership
  boundary; and
- two production `taskkill /T /F` execution paths.

The resulting ownership shape was inconsistent:

```text
cmd/BAT (returns)
  `- pythonw launch_guard + GUI (not a lifecycle boundary)
       |- some workers -> private Job
       |- other workers -> raw Popen/run
       `- folders/manual tools -> raw shell launch
```

Stopping the BAT or losing the GUI therefore did not provide one contract that
accounted for every tool-owned child and grandchild.

## After

The common ownership shape is:

```text
cmd/BAT (process-creation handoff, then returns)
  `- launch_guard + GUI supervisor
       `- session Job (KILL_ON_JOB_CLOSE)
            |- private Job A -> owned root A -> owned descendants
            |- private Job B -> owned root B -> owned descendants
            `- ...

outside every Job:
  |- explicit manual/standalone/shell handoffs
  `- pre-existing or unrelated external processes
```

Every owned root is created suspended, assigned session-first/private-second,
written to an atomic receipt with PID plus creation `FILETIME`, and only then
resumed. Stop operates on the retained private Job handle. Supervisor loss
closes its non-inheritable Job handles. There is no executable-name discovery,
PID-only kill, or production `taskkill` path.

## Sanitized runtime results

The 15 Windows lifecycle tests proved these identity-checked outcomes:

| Scenario | Owned parent/grandchild | Concurrent owned sibling | External sibling | Receipt |
| --- | --- | --- | --- | --- |
| Normal completion / GUI close | both absent by PID + creation `FILETIME` | n/a | n/a | `process_tree_clean`, `survivors=[]` |
| Cooperative Stop | both exit during bounded stop-file callback | n/a | n/a | no forced result |
| Forced Stop | only the selected private Job becomes empty | remains alive | remains alive | forced result names private-Job termination |
| Root crash with silent grandchild | grandchild removed after bounded grace | n/a | n/a | `process_tree_forced_after_root_exit` |
| Forced supervisor termination | both removed by session Job close | n/a | remains alive | recovered receipt uses `survivors=null`; exit was not retrospectively claimed |
| Two concurrent owned roots | Stop A removes only A's tree | B remains until session close, then exits | n/a | exact per-launch records |
| Launch concurrent with shutdown | suspended child is fully registered before shutdown snapshot | n/a | n/a | no launch escapes the closing session |

Additional regressions cover a child creating nested Jobs, three repeated
launch/Stop cycles with no growth, BAT interruption after process creation,
non-inheritable Job handles, rejected breakaway flags, Windows 7 fail-closed
gating, query/termination exit races, PID reuse, and uncertain owner-liveness
queries that leave receipts unrecovered.

## Final verification

- compile gate: 196 Python sources, 4 contract groups;
- static launch/Job ownership audit: 4/4;
- Windows lifecycle acceptance suite: 15/15;
- all discovered suites: 1,607/1,607;
- `compileall`: passed; and
- test-before/test-after worktree comparison: no side effects.
