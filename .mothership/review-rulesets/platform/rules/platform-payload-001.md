---
schema: 2
id: PLATFORM-PAYLOAD-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
provenance: application-sdk docs/upgrade-guide-v3.md (Temporal payload limit)
suppressible: false
---
# Activity payloads carry references, not data

- Temporal caps each payload at ~2 MB and the whole history at ~50 MB.
- MUST pass object-store paths / file references in activity inputs and
  outputs — never row data, asset lists, or accumulated results.
- FLAG any activity output whose size scales with source size (a list of
  tables, a dict of per-entity counts over an unbounded catalog): it works
  in dev and explodes on a large tenant.
- Workflow return values and signal/query payloads are under the same cap.
