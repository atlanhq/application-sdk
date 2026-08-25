---
schema: 2
id: PLATFORM-RESOURCES-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# atlan.yaml resources are a coupled contract with the code

The deploy config (workerResources requests/limits, KEDA scaling, VPA
bounds, PVC size, activity concurrency, per-engine memory env vars) must
stay consistent with what the code allocates — drift shows up as pod
eviction, OOMKill, or disk exhaustion on large tenants, never in CI.

- Re-verify the math when either side changes: max concurrent activities ×
  (per-activity engine memory + runtime RSS) fits the pod limit with
  headroom.
- FLAG a code change that raises per-activity memory, adds a spill/temp
  write, or increases fan-out without a matching atlan.yaml review — and
  an atlan.yaml change with no stated capacity math.
- New local-disk writes are checked against PVC size and the end-of-run
  cleanup story.
- Indirect cases: a dependency upgrade changing an engine's memory
  defaults; scale-down racing long activities (eviction retry budget); a
  new activity type sharing a worker sized for cheaper work.
