---
schema: 2
id: APP-OBSERVABILITY-001
level: L2
category: observability
globs: []
severity: MEDIUM
suppressible: true
---
# Bounded observability

- No per-row or per-batch logging on hot paths — log and measure at phase
  boundaries with counts.
- Metric labels come from bounded sets only: never workflow IDs, row
  counts, or `str(e)` as a label value (cardinality bomb).
- FLAG always-on background diagnostic loops added "temporarily".
