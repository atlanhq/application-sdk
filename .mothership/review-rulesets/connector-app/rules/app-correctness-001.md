---
schema: 2
id: APP-CORRECTNESS-001
level: L2
category: correctness
globs: []
severity: HIGH
provenance: sdk-v3-connector-review-guidelines; completeness-verification patterns
suppressible: false
---
# Publish complete results or fail loudly

- MUST fail the run when any extraction slice is missing — never publish a
  partial result as a complete one. Downstream diffing turns silent
  under-extraction into asset deletes.
- MUST verify cross-activity handoffs against a declaration (expected file
  list/count written by the producer), not by listing whatever exists at a
  storage prefix. A prefix scan cannot tell "absent" from "lost".
- MUST keep completeness checks per source; never sum counts across sources —
  a surplus in one producer must not mask a shortfall in another.
- FLAG any `except` that converts a missing input into an empty result.
