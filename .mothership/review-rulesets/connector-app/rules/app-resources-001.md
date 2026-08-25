---
schema: 2
id: APP-RESOURCES-001
level: L2
category: correctness
globs: []
severity: MEDIUM
suppressible: false
---
# Client and handle lifecycle

- Expensive clients (DB engines, HTTP sessions, drivers) are created once
  per worker/handler, not per task invocation — pool exhaustion under retry
  is a real production failure mode.
- Every cursor, connection, file handle, and temp artifact closes in
  `finally` or a context manager so retries do not leak.
- Every variable used in a `finally` block (or after a `try`) is
  initialized BEFORE the `try` — `UnboundLocalError` on the failure path
  masks the original error.
- FLAG cleanup that can itself raise inside `finally` without a guard.
