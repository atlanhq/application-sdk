---
schema: 2
id: PLATFORM-HISTORY-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Respect the workflow history budget

- History is hard-capped (~51,200 events); every activity schedule, retry,
  and heartbeat-timeout cycle appends events.
- The number of activities, chunks, and batches MUST be bounded by design,
  not by input size — an uncapped chunker works in dev and dies on a large
  tenant, usually surfacing as a misattributed heartbeat timeout.
- FLAG workflow-body loops whose iteration count scales with tenant size;
  they need batching or continue-as-new before they ship.
- Retry policies multiply events: unbounded retries on fanned-out
  activities are a history bomb.
