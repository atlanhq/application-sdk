---
schema: 2
id: PLATFORM-RESOURCES-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
provenance: connector atlan.yaml deploy contract (workerResources/KEDA/VPA/PVC)
suppressible: false
---
# atlan.yaml resources are a coupled contract with the code

`atlan.yaml`'s deploy section (workerResources requests/limits, KEDA
minReplicaCount/targetQueueSize, VPA bounds, PVC storage size, activity
concurrency, per-engine memory env vars) must stay consistent with what the
code actually allocates — drift shows up as pod eviction, OOMKill, or
"No space left on device" on large tenants, not in CI.

- MUST re-verify the concurrency math when either side changes:
  max concurrent activities × (per-activity engine memory + Python RSS)
  must fit inside the pod memory limit with headroom.
- FLAG a code change that raises per-activity memory, adds a new spill/temp
  write, or increases fan-out WITHOUT a matching atlan.yaml review — and an
  atlan.yaml resource/scaling change with no stated capacity math.
- FLAG new local-disk writes against the PVC size and cleanup story;
  spill directories must be bounded and wiped at end of run.
- Timeouts (`ATLAN_HEARTBEAT_TIMEOUT_SECONDS`, start-to-close) changed as a
  "fix" for a slow activity are a smell — see PLATFORM-HEARTBEAT-001.
- Indirect cases: a dependency upgrade that changes an engine's default
  memory behavior; KEDA scale-down racing long activities (eviction retry
  budget); a new activity type sharing a worker whose slots were sized for
  cheaper work.
