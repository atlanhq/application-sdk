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
  results for exceptions — a dropped exception silently loses one slice of
  the extraction (see APP-CORRECTNESS-001).
- Safe path: letting the first failure propagate deliberately is fine;
  gather-and-ignore is not.
- FLAG unbounded fan-out (one task per item over an unbounded collection);
  concurrency is bounded by design, not by input size.
