---
schema: 2
id: APP-CORRECTNESS-001
level: L2
category: correctness
globs: []
severity: HIGH
suppressible: false
---
# Never publish undeclared loss

The sin is SILENT incompleteness: output missing slices while claiming
SUCCESS. Downstream diffing turns silent under-extraction into asset
deletes. Declared degradation is sanctioned — the SDK's
`OutputStatus.PARTIAL_SUCCESS` exists exactly for "some entities skipped
or degraded, output still usable", and the degradability axis (see the
errors rule) decides what MAY be absorbed.

- A missing REQUIRED slice with no declaration is a finding: the run must
  either fail or report PARTIAL_SUCCESS with the gap named.
- MUST verify cross-activity handoffs against a producer-written
  declaration (expected file list/count), never by listing a storage
  prefix — a prefix scan cannot tell "absent" from "lost".
- Completeness counts are per source, never summed — a surplus in one
  producer must not mask a shortfall in another.
- Reads distinguish missing from corrupt; converting a missing input into
  an empty result under a SUCCESS status is how a delete gets published.
- FLAG any `except` that degrades a missing fundamental input to an empty
  frame, zero rows, or a skipped slice without touching the run status.
- Safe path: an intentional coverage change stated in the PR, with the
  declaration/count logic changed alongside.
