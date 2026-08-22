---
schema: 2
id: PLATFORM-HISTORY-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Respect the workflow history budget

- Every activity schedule/retry/completion appends history events; Temporal
  hard-terminates a workflow around 50K events / 50 MB history.
- MUST NOT schedule one activity per item over an unbounded collection
  (per-table, per-schema, per-asset). Batch the collection into a bounded
  number of activities sized by the source.
- FLAG loops in a workflow body whose iteration count scales with tenant
  size; they need batching or continue-as-new before they ship.
- Retry policies multiply events: an unbounded retry on a fanned-out
  activity is a history bomb.
