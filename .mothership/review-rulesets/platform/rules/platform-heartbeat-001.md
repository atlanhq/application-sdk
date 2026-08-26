---
schema: 2
id: PLATFORM-HEARTBEAT-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Heartbeats must survive synchronous work

The conformance suite flags direct blocking calls inside `async def`
mechanically. This rule owns the indirection and the timeout judgment:

- Blocking work hidden one or more calls away — a sync driver call inside
  a helper that an async activity awaits — blocks the loop just the same.
  A blocked loop stalls every concurrent activity on the worker AND stops
  SDK auto-heartbeats, so the activity dies as a misleading heartbeat
  timeout. Trace the call chain when an async path changes.
- The sanctioned offload (`run_in_thread`) keeps the loop — and therefore
  auto-heartbeats — running; do NOT demand manual heartbeats inside it.
  Manual heartbeat details are needed only for progress/resume semantics
  (checkpointing partial work across retries).
- Offloaded blocking work MUST carry its own internal timeout (the
  driver's, the HTTP client's). The framework cannot safely kill a
  thread, and the offload sits in an unbounded progress hold — a hung
  call without an internal timeout runs forever without ever reading as
  a stall. Bounding via `holding_progress(timeout=...)` is the sanctioned
  alternative when the callee has no timeout parameter.
- Tight parse/transform loops inside async generators do not yield;
  long iterations need explicit yield points.
- MUST NOT "fix" a heartbeat timeout by raising the timeout value — find
  the blocking section. Timeouts sized to the p99 of real work, stated in
  the PR when changed.
