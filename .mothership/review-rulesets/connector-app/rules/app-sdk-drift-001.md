---
schema: 2
id: APP-SDK-DRIFT-001
level: L2
category: correctness
globs: []
severity: MEDIUM
suppressible: true
---
# Use the SDK helper, not a hand-rolled copy

- FLAG new code that reimplements functionality the SDK already provides —
  logging setup, state/storage access, HTTP or source clients, retry
  wrappers, config resolution, heartbeating, temp-file management.
- Bespoke infrastructure fragments behavior across the fleet and blocks
  centralized fixes: a bug fixed in the SDK stays alive in every copy.
- Safe path: a documented SDK gap — the PR names the missing capability
  and links the SDK issue; the local shim is marked for removal.
- Check the SDK's public helpers before accepting "the SDK cannot do this"
  claims in the diff or PR description.
