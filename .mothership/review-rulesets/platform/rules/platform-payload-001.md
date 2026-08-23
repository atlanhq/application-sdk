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

- Payloads are capped (~2 MB each) and the whole workflow history is
  capped — activity inputs/outputs carry object-store paths or
  FileReferences, never row data, asset lists, or accumulated results.
- FLAG any activity output whose size scales with source size (a list of
  tables, a per-entity dict over an unbounded catalog) — small in dev,
  fatal on a large tenant, and the failure surfaces as an unrelated
  timeout, not a payload error.
- Credentials cross activity boundaries as references, never raw values —
  a raw credential in a payload persists in workflow history.
- Workflow return values and signal/query payloads sit under the same cap.
