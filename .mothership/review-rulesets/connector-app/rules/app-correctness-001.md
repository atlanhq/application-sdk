---
schema: 2
id: APP-CORRECTNESS-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Publish complete results or fail loudly

- MUST fail the run when any extraction slice is missing — never publish a
  partial result as complete. Downstream diffing turns silent
  under-extraction into asset deletes.
- MUST verify cross-activity handoffs against a producer-written declaration
  (expected file list/count), never by listing a storage prefix — a prefix
  scan cannot tell "absent" from "lost".
- Completeness counts are per source, never summed — a surplus in one
  producer must not mask a shortfall in another.
- Reads distinguish missing from corrupt; converting a missing input into an
  empty result is how a delete gets published.
- Safe path: an intentional coverage change is fine when the PR states it
  and the declaration/count logic changes with it.
- FLAG any `except` that degrades a missing fundamental input to an empty
  frame, zero rows, or a skipped slice.
