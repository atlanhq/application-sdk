---
schema: 2
id: APP-PARALLEL-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Fan-out failures must surface

- MUST use `asyncio.gather(..., return_exceptions=True)` AND inspect the
  results for exceptions — a dropped exception silently loses a slice of the
  extraction (see APP-CORRECTNESS-001).
- Alternatively let the first failure propagate deliberately — but never
  gather-and-ignore.
- FLAG unbounded fan-out (one task per item over an unbounded collection);
  batch with a bounded concurrency primitive.
