---
id: FP06
applies-to: [app]
---
# Long work reports progress

Principle: A task that can run long shows progress the SDK can see. A custom loop calls
`self.heartbeat(...)` as each unit completes. One long call the SDK cannot see into is wrapped in
`holding_progress(label, timeout=...)`, where the timeout is how long that one call may take.
Why: A healthy but quiet task looks the same as a stuck one. Without a signal, the stall watchdog
fails good work after the no-progress budget, or a stuck call holds a worker slot until the task's
overall timeout.
Ask:
- Can one step in the changed task (a query, an API call, a page, a transform pass) run longer than the no-progress budget (900 seconds by default) with no heartbeat or hold?
- Is each hold's timeout the longest that one call should be allowed to run, set where the call is made, and not a guess at how long the whole task takes?
- Does the diff raise the no-progress budget or a timeout to make a stall go away, without saying what real work needs that long?
Don't flag: work the SDK already reports: `run_in_thread` offloads, SDK writers, transfers and the
SDK's own page loops. Conformance reports a blocked event loop (`P023`, `P065`) and preflight
deadlines (`F012`), and FP05 covers blocking calls. Do not ask for heartbeats in short tasks.
Severity: High when a normal large run would be failed as stalled, or a stuck call would never be
noticed. Medium for a missing or badly sized hold on a step that is rarely slow.
