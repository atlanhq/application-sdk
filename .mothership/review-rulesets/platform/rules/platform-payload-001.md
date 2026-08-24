---
schema: 2
id: PLATFORM-PAYLOAD-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Activity payloads carry references, not data

The conformance suite flags the mechanical shapes (bytes-typed contract
fields, str paths that should be FileReferences, unbounded-field opt-outs).
This rule owns the scaling judgment:

- FLAG any activity output whose SIZE scales with source size — a list of
  tables, a per-entity dict over an unbounded catalog. It is small in dev,
  fatal on a large tenant (~2 MB per payload), and the failure surfaces
  as an unrelated timeout, not a payload error.
- Workflow return values and signal/query payloads sit under the same
  cap; accumulating per-batch results into the workflow's return value is
  the same bug in a different position.
- Credentials cross activity boundaries as references, never raw values —
  a raw credential in a payload persists in workflow history.
