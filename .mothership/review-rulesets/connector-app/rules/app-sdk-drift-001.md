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

The conformance suite flags the KNOWN hand-rolled shapes (own storage
clients, manual Temporal workers, manual HTTP servers, bespoke process
isolation, hand-rolled upload bridges). This rule owns the unnamed ones:

- FLAG new code that reimplements SDK-provided behavior the suite has no
  check for yet — bespoke retry wrappers, config resolution, logging
  setup, temp-file management, heartbeat plumbing.
- Check the SDK's public helpers before accepting an "the SDK cannot do
  this" claim in the diff or PR description.
- Safe path: a documented SDK gap — the PR names the missing capability
  and links the SDK issue; the local shim is marked for removal.
- Bespoke infrastructure fragments the fleet: a bug fixed in the SDK
  stays alive in every private copy.
