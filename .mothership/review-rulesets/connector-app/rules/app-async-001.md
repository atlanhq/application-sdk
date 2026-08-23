---
schema: 2
id: APP-ASYNC-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Never block the event loop

- MUST NOT call sync I/O inside `async def` — blocking driver calls,
  `requests.*`, sync DB clients, large `open()` reads, `subprocess.run`.
- MUST NOT call `time.sleep()` in async code; use `asyncio.sleep()`.
- A blocked loop stalls every concurrent activity on the worker AND stops
  SDK auto-heartbeats — the activity dies as a misleading heartbeat timeout.
- Safe path: offload via `asyncio.to_thread` / the SDK's `run_in_thread`,
  AND give the blocking call its own internal timeout — the framework
  cannot safely kill a thread that never returns.
- FLAG a raised heartbeat-timeout value used as the "fix" for a blocking
  call — make the work non-blocking instead.
