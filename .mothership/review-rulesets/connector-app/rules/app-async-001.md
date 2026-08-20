---
schema: 2
id: APP-ASYNC-001
level: L2
category: correctness
globs: []
severity: HIGH
provenance: atlan-databricks-app app-review.md (ASYNC, SDK v3 platform rules)
suppressible: false
---
# Never block the event loop

- MUST NOT call sync I/O inside `async def` — blocking driver calls,
  `requests.*`, large `open()` reads, `subprocess.run`. Offload with
  `asyncio.to_thread` or use an async client.
- MUST NOT call `time.sleep()` in async code; use `asyncio.sleep()`.
- A blocked event loop stalls every concurrent activity on the worker AND
  stops SDK auto-heartbeats, so the activity is killed by heartbeat timeout.
- FLAG a raised heartbeat-timeout value used as the "fix" for a blocking
  call — make the work non-blocking instead.
