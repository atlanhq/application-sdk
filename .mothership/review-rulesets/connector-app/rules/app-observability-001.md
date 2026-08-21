---
schema: 2
id: APP-OBSERVABILITY-001
level: L2
category: observability
globs: []
severity: MEDIUM
provenance: sdk-v3-connector-review-guidelines
suppressible: true
---
# Bounded observability

- MUST NOT log per-row or per-batch on hot paths — log/metric at phase
  boundaries with counts.
- Metric labels are bounded sets only: never `workflow_id`, row counts, or
  `str(e)` as a label value (cardinality bomb).
- FLAG always-on background diagnostic loops added "temporarily".
