---
schema: 2
id: APP-ASYNC-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Blocking the event loop: the cases the suite cannot see

The conformance suite flags direct blocking calls inside `async def`
mechanically. This rule owns what static analysis cannot reach:

- Blocking work hidden one or more calls away — a sync driver call inside
  a helper that an async activity awaits. Trace the call chain when an
  async path's latency profile changes.
- A blocked loop stalls every concurrent activity on the worker AND stops
  SDK auto-heartbeats — the activity dies as a misleading heartbeat
  timeout. Use this consequence when judging severity.
- Offloaded work (`asyncio.to_thread` / `run_in_thread`) MUST carry its
  own internal timeout — the framework cannot safely kill a thread that
  never returns. The offload alone is not the fix.
- FLAG a raised heartbeat-timeout value used as the "fix" for a blocking
  call — make the work non-blocking instead.
