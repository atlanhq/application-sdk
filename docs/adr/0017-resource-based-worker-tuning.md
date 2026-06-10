# ADR-0017: Resource-Based Worker Tuning

## Status
**Accepted**

## Context

Workers hand out *activity slots*: each slot is one activity the worker is
willing to run concurrently. Today the slot count is fixed —
`TEMPORAL_MAX_CONCURRENT_ACTIVITIES`, default `100` — regardless of how big
the pod actually is.

A fixed slot count is resource-blind:

- A single activity can need GBs of memory (parquet transforms, large query
  result batches), while another needs a few MB. The fixed count cannot tell
  them apart — 100 concurrent heavy activities on a small pod OOMs the
  worker.
- To stay safe, pods are sized for **worst-case fanout**: the memory the pod
  would need if all 100 slots filled with the heaviest activity at once.
  Most of the time only a handful of slots are in use, so the reservation is
  pure waste.
- VPA cannot right-size its way out: because peak usage is determined by the
  (fixed) slot count rather than the actual workload, VPA's recommendation
  must also cover the worst-case fanout, locking in the over-provisioning.

temporalio 1.28.0 ships a resource-based `WorkerTuner`
(`WorkerTuner.create_resource_based(target_memory_usage=...,
target_cpu_usage=...)`): instead of a fixed slot count, the worker throttles
slot handout based on *observed* CPU and memory usage, targeting a
configured fraction of the pod's resources. Small pods naturally run fewer
concurrent activities; large pods run more — without anyone tuning a number
per app. Temporal forbids combining `tuner=` with the fixed
`max_concurrent_activities` / `max_concurrent_workflow_tasks` arguments
(`Worker(...)` raises `ValueError`), so the two modes are mutually
exclusive by construction.

## Decision

We add an **opt-in resource-based tuner**, selected via environment
variables read by `load_execution_settings()`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ATLAN_WORKER_TUNER_MODE` | `fixed` | `fixed` (static slot counts, unchanged behavior) or `resource` (resource-based tuner) |
| `ATLAN_WORKER_TUNER_TARGET_MEMORY` | `0.8` | Target fraction `(0, 1]` of system memory the tuner keeps in use |
| `ATLAN_WORKER_TUNER_TARGET_CPU` | `0.9` | Target fraction `(0, 1]` of system CPU the tuner keeps in use |

In `resource` mode, `create_worker()` passes `tuner=` to Temporal's
`Worker(...)` and omits the fixed `max_concurrent_*` arguments. Explicit
caller-supplied slot counts take precedence over the env flag (with a
warning) so flipping the platform default can never crash an app that pins
slot counts in code.

The default stays `fixed` **for now**: flipping the fleet default to
`resource` is planned after canary validation using the per-activity CPU
and memory cost metrics (`activity cost` observability shipped alongside
this change), which give us the per-activity-type resource profiles needed
to confirm the tuner's throttling matches reality.

## Options Considered

### Option 1: Keep Fixed Slots, Lower the Default (Not Chosen)

Reduce `TEMPORAL_MAX_CONCURRENT_ACTIVITIES` from 100 to something small.

**Pros:**
- Trivial change, no new code paths
- Predictable, easy-to-reason-about concurrency

**Cons:**
- Still resource-blind: the right number differs per app, per pod size, and
  per activity mix — one default cannot fit all
- Lowering it throttles big pods that could safely run more, hurting
  throughput exactly where capacity exists
- Keeps the worst-case-fanout sizing problem; just moves the cliff

### Option 2: Resource-Based WorkerTuner, Flag-Gated (Chosen)

Use `WorkerTuner.create_resource_based()` behind `ATLAN_WORKER_TUNER_MODE`,
defaulting to `fixed`.

**Pros:**
- Slot handout adapts to the pod's actual CPU/memory — small pods stop
  OOMing, large pods are no longer artificially throttled
- Pods can be sized for typical load instead of worst-case fanout; VPA
  recommendations become meaningful
- Opt-in rollout: zero behavior change until an app (or the platform)
  flips the flag; default flip planned after canary validation
- No per-app tuning of slot counts required

**Cons:**
- Concurrency becomes less predictable run-to-run (it tracks observed load)
- Targets (`0.8` memory / `0.9` CPU) are heuristics; a pathological activity
  that allocates faster than the tuner samples can still spike past the
  target
- One more mode to reason about in incident debugging

### Option 3: Custom Per-Activity-Type SlotSupplier (Future Work)

Implement a custom `SlotSupplier` that budgets slots from per-activity-type
resource profiles (e.g. "this activity type costs ~2 GB; admit only as many
as fit").

**Pros:**
- Most precise: admission control keyed to what each activity actually costs
- Composable with the per-activity cost metrics we already emit

**Cons:**
- Requires trustworthy per-activity-type profiles fleet-wide before it can
  ship — exactly what the canary phase of Option 2 will produce
- Significantly more code to own (admission logic, profile storage, decay)
- Premature until the simple resource-based tuner is proven insufficient

## Rationale

1. **Cost**: worst-case-fanout pod sizing is the dominant waste. Making
   concurrency follow the pod's real resources removes the need for it.
2. **Safety first**: default `fixed` keeps every existing deployment
   byte-for-byte unchanged; `resource` is validated on canaries before any
   default flip.
3. **Leverage, not invention**: Temporal's tuner is upstream-maintained and
   already handles the slot/throttle mechanics; a custom SlotSupplier stays
   available as a sharper tool later.

## Consequences

**Positive:**
- Small pods stop OOMing under fanout; large pods regain throughput
- Pods and VPA can right-size to typical load; VPA recommendations are no
  longer dominated by the fixed-slot worst case
- Per-app slot tuning disappears in `resource` mode

**Negative / Watch-outs:**
- **KEDA `targetPendingActivities` interaction**: KEDA scales worker
  replicas on task-queue backlog. In `resource` mode a saturated worker
  admits fewer activities, so backlog grows *earlier* on small pods — KEDA
  will scale out sooner. That is usually the desired behavior (scale out
  instead of OOM), but scaler thresholds tuned against the fixed 100-slot
  assumption should be revisited when a fleet flips to `resource`.
- **VPA right-sizing loop**: VPA shrinking a pod now also shrinks its
  concurrency, which shifts load to KEDA scale-out. The steady state is more
  smaller pods rather than few over-provisioned ones; deployments that must
  bound replica count should pin KEDA `maxReplicaCount` accordingly.
- In `resource` mode the `worker_start` event reports
  `max_concurrent_activities: null` — dashboards keying on that field must
  treat it as "dynamic", not zero.
- Temporal forbids `tuner=` together with `max_concurrent_*`; the SDK
  resolves the conflict by letting explicit code-level slot counts win and
  logging a warning.

## Implementation

```bash
# opt a deployment into resource-based tuning
ATLAN_WORKER_TUNER_MODE=resource
ATLAN_WORKER_TUNER_TARGET_MEMORY=0.8   # optional, default 0.8
ATLAN_WORKER_TUNER_TARGET_CPU=0.9     # optional, default 0.9
```

`create_worker()` (`application_sdk/execution/_temporal/worker.py`) reads
`ExecutionSettings.worker_tuner_mode` and either builds
`WorkerTuner.create_resource_based(...)` or forwards the fixed
`max_concurrent_*` slot counts — never both. The worker logs the active
tuning mode at startup
(`Worker tuning mode: resource (...)` / `Worker tuning mode: fixed (...)`).
