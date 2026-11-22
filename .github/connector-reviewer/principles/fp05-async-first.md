---
id: FP05
applies-to: [app]
---
# Async first; blocking work is bounded and can be cancelled

Principle: Nothing blocks the event loop. Use an async library first. When a call has no async
form, offload it with `run_in_thread`, give it a timeout the driver itself enforces, and make sure
a cancelled task can stop it.
Why: A blocked loop stops heartbeats for every task on the worker, so healthy work dies as a
heartbeat timeout. An offloaded call with no timeout or cancel keeps its thread, connection and
memory after the task is gone, because Python cannot stop a thread from outside.
Ask:
- Follow each new or changed call on an async path through its helpers: does it end in a sync driver, client or file call, or in a long loop that never awaits?
- For each offloaded call: which timeout inside the driver ends it, and what stops it on cancel (a registered driver cancel, or `is_cancelled()` checked between steps)?
- Does the diff raise a heartbeat or task timeout instead of finding what blocks?
Don't flag: direct cases conformance reports: a blocking call in an `async def` (`P023`), the
shared default executor (`P031`), a `with`-scoped pool joined on cancel (`P054`), an offloaded
cursor statement with no cancel (`P064`), batched results read on the loop (`P065`), the sync Atlan
client (`P024`), preflight probes (`F011`). Do not ask for manual heartbeats inside
`run_in_thread`.
Severity: High when a call can block the loop, or hang with no bound, on a production path. Medium
for a missing cancel on a call that a driver timeout already bounds.
