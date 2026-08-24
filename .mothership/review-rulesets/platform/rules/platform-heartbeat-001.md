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
mechanically. This rule owns the indirection and the lifecycle judgment:

- Blocking work hidden one or more calls away — a sync driver call inside
  a helper that an async activity awaits — blocks the loop just the same.
  A blocked loop stalls every concurrent activity on the worker AND stops
  SDK auto-heartbeats, so the activity dies as a misleading heartbeat
  timeout. Trace the call chain when an async path changes.
- Work offloaded to a thread (`asyncio.to_thread` / `run_in_thread`) MUST
  heartbeat explicitly inside the loop body AND carry its own internal
  timeout — the framework cannot safely kill a thread, and a thread does
  not yield to the loop on its own.
- Tight parse/transform loops inside async generators do not yield
  either; long iterations need explicit heartbeat/yield points.
- MUST NOT "fix" a heartbeat timeout by raising the timeout value — find
  the blocking section. Timeouts sized to the p99 of real work, stated in
  the PR when changed.
