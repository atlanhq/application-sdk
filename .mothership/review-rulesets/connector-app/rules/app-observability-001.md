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

The conformance suite owns logging mechanics (f-strings, tight-loop
logging, exc_info, logger factories). This rule owns the judgment cases:

- Metric labels come from bounded sets only: never workflow IDs, row
  counts, or `str(e)` as a label value — unbounded label sets are a
  cardinality bomb that static checks cannot size.
- FLAG always-on background diagnostic loops added "temporarily" — they
  outlive their incident.
- Signal placement: counts and timings belong at phase boundaries; a log
  line that scales with data volume needs a stated reason.
