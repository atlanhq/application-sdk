---
schema: 2
id: APP-RESOURCES-001
level: L2
category: correctness
globs: []
severity: MEDIUM
provenance: sdk-v3-connector-review-guidelines
suppressible: false
---
# Client and handle lifecycle

- Create expensive clients (DB engines, HTTP sessions, drivers) once per
  worker/handler, not per task invocation — pool exhaustion under retry is a
  real production failure mode.
- Every cursor, connection, file handle, and temp artifact is closed in
  `finally` (or a context manager) so retries do not leak.
- FLAG cleanup that can itself raise inside `finally` and mask the original
  error — guard it.
