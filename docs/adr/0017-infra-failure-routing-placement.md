# ADR-0017: Infra-Failure Routing and Interpreter Placement

## Status
**Proposed**

## Context

Apps run on spot (preemptible) nodes by default for cost. A spot reclaim, OOMKill,
or node loss kills the worker process abruptly, before any in-process handler can
run. The worker reports nothing; the activity stops heartbeating, and Temporal fails
the attempt with a **heartbeat timeout** - which is retryable and **consumes
`max_attempts`** (see `docs/worker-eviction-and-sigterm.md` for why abrupt kills
never report `WorkerEvicted`). A workflow on spot can hit this repeatedly, burning
its retry budget and eventually failing, with every retry landing back on the same
spot queue.

**Goal of this ADR:** when an activity dies from an infra event, retry it on an
**on-demand task queue** so the work can finish, instead of re-failing on spot.

**Prior work.** PR #2462 scaffolds detection + a fail-safe `diagnose_infra_failure`
activity + a `classify_infra_failure` policy stub, all in the SDK
(`docs/infra-failure-diagnosis.md`), flag-gated off. It stops short of routing
because a second on-demand worker pool does not exist yet.

This ADR responds to the design-review request (thread on the worker-eviction doc)
for a full comparison of where this logic should live: in the SDK, in a Temporal
extension, in a standalone service, or in the Automation Engine (AE).

## Scope

**This ADR decides** where the *interpreter* lives - the layer that turns an infra
event into a queue-routing action - and the shape of the SDK ↔ recorder interface.

**Out of scope:**

- **Attribution / failure-cause surfacing.** Handled by AE, not the SDK (premise P4).
- **The recorder's internals.** A net-new, platform-owned service; this ADR only
  consumes its signal.

## Settled Premises

Four points are not in contention; they frame the actual question.

- **P1 - The signal comes from an external recorder.** Temporal has zero Kubernetes
  awareness: it sees a bare heartbeat timeout and cannot tell an OOM from a spot
  reclaim. The dead pod cannot self-report, and the next attempt runs on a
  *different* pod, so nothing in-process knows the cause. A live k8s query is
  unreliable (pods GC'd, Events expire ~1h). So the cause must come from a durable,
  out-of-band **recorder** that ingests spot notices, pod `lastState.terminated`
  (OOM/137), and node events (Karpenter/NTH).

- **P2 - Routing lives in application-sdk.** Only workflow code can move work between
  queues: a `RetryPolicy` never changes task queue; a fresh
  `execute_activity(..., task_queue=...)` does. Every unit AE spawns is a separate
  SDK-based app that needs the *identical* behaviour, so it belongs in the shared
  substrate - the SDK - not in any one app or the engine above.

- **P3 - No Kubernetes code in the SDK.** All k8s-aware logic (event ingestion,
  pod/node watching, `lastState`/Karpenter parsing, k8s API calls) lives in the
  recorder/controller. The SDK reaches it only over HTTP (wrapped in a fail-safe
  activity). The SDK's sole *local* infra touch-point is reading `NODE_NAME` /
  `POD_NAME` from downward-API env for correlation - Helm-wired config, not a k8s
  call. "No k8s code" is not "no external call": correct routing genuinely needs the
  lookup.

- **P4 - Attribution is AE's job.** Workflows are orchestrated through
  `../atlan-automation-engine-app`, which spawns each app as a Temporal **child
  workflow** and walks the failure chain `ChildWorkflowError → ActivityError →
  ApplicationError(details)` to the leaf activity failure
  (`automation_engine/utils/temporal_errors.py`). Holding the last activity's failure
  reason, AE can query the recorder itself and record the cause - even after the app
  workflow has exited, which the SDK cannot do. So the SDK does **not** need to
  design attribution.

### Alternatives ruled out (answering the review's option list)

- **Host routing in AE.** AE observes only at the child-workflow boundary, *after*
  an app has exhausted its retries and failed - too coarse (whole-app, not
  per-activity) and too late (cannot save the run). AE stays the attribution
  consumer (P4), not the routing host.
- **Temporal extension / interceptor in the SDK.** Pulls infra-detection machinery
  deeper into the SDK (against P3), and interceptors wrap calls under determinism
  constraints - they cannot express "catch this `ActivityError`, then re-dispatch on
  another queue," so routing ends up as explicit workflow code anyway.
- **Recorder baked into AE.** Couples a cluster-infra concern to the orchestration
  engine's release cadence, and forces the SDK's routing lookup to depend on AE's
  availability. The recorder is a standalone platform service instead.

## The Open Question

Given P1-P4, one design choice remains: **who interprets the infra event into a
routing action, and what does the recorder return?**

- **Cause vocabulary** (infra facts): `SPOT_RECLAIM`, `OOM_KILLED`, `NODE_LOST`,
  `NONE`.
- **Action vocabulary** (SDK/Temporal semantics): `REROUTE_ONDEMAND`, `FAIL_FAST`,
  `RETRY_SAME` (today's behaviour).

The interpreter is the cause → action mapping. It can live in the SDK, in a
separate service, or inside the recorder.

## Approaches

### Approach 1 - Interpret in the SDK (Recommended)

The recorder returns the raw `{cause, evidence}`. On a heartbeat timeout the SDK
correlates the dead pod, calls the recorder (fail-safe activity), maps cause → action
with a fixed policy, and executes.

```
 heartbeat_timeout ─► SDK correlates {node,pod,window}
                       └─► recorder.lookup() ─► {cause}
                            └─► SDK policy: cause → action → execute
                                 SPOT/NODE_LOST → reroute on-demand
                                 OOM            → fail-fast
                                 NONE / down    → retry same (today)
```

**Pros:**
- Recorder stays single-responsibility - a pure infra fact store. The same simple
  contract serves both consumers: AE (attribution) and the SDK (routing). No
  orchestration semantics leak into an infra service.
- The action vocabulary is SDK/Temporal knowledge; the component that *executes* the
  action also *decides* it, so it can never be handed an action it cannot perform.
  No capability negotiation, no version skew.
- Cohesive change surface: adding or altering an action is one SDK change, beside the
  mechanism that implements it.
- The scaffold already implements this shape (`classify_infra_failure`); lowest
  friction.

**Cons:**
- The (app-agnostic) routing policy ships in the SDK, so changing it needs an SDK
  release across apps. Low cost in practice - the policy is uniform and stable, and a
  fixed default is acceptable (P2).
- No fleet-wide *runtime* control of routing behaviour unless a flag/config is added
  (addressed in the recommendation).

### Approach 2 - Interpret in an external service (returns an action primitive)

The recorder returns raw causes; a separate interpreter service maps cause → action
and hands the SDK a ready-made **action primitive**. The SDK just executes it.

**Pros:**
- Policy centralised: change routing behaviour with one deploy, no SDK bump.
- Fleet-wide runtime control - platform can flip behaviour mid-incident (e.g. stop
  rerouting when on-demand is *also* constrained) instantly.
- SDK is a thin, stable executor of a small primitive set.

**Cons:**
- Leaks orchestration semantics into an infra-adjacent service: to emit a correct
  primitive it must understand SDK task-queue topology and retry budgets - the wrong
  knowledge boundary.
- **Version skew:** the service can emit a primitive a deployed SDK version does not
  support; needs a capability contract or a safe-ignore rule. More brittle than the
  executor owning its own actions.
- Demotes the SDK from orchestrator to actuator, in tension with P2. And it does not
  actually decouple: the action primitive is still SDK-specific, so externalizing the
  mapper *inverts* the coupling rather than removing it - an infra-adjacent service now
  reasons about SDK orchestration, the wrong direction. In-SDK the coupling runs the
  natural way: the orchestrator consumes an infra vocabulary it already understands.
- Adds a second synchronous decision dependency on the failure path (beyond the fact
  lookup).
- Fleet-wide blast radius: a bad or stale policy push misroutes every app at once -
  fast to fix, but fast to break, and it needs staged-rollout guardrails on the policy
  itself. In-SDK, a bad mapping ships via a normal SDK release (reviewed, staged,
  rolled out per app), so it cannot break the whole fleet in a single push.

### Approach 3 - Fold the interpreter into the recorder (recorder returns the action)

One call: the recorder itself returns the action primitive.

**Pros:**
- One fewer hop / service than Approach 2; still centralises policy and runtime
  control.

**Cons:**
- Destroys the recorder's single responsibility - it must now know SDK orchestration
  to decide actions - and couples its release cadence to routing-policy changes.
- Serves AE poorly: AE wants raw facts for attribution, so the recorder must either
  emit SDK-specific actions AE ignores, or maintain two response shapes.
- Inherits Approach 2's semantic-leak and version-skew cons, now baked into the
  shared fact store.

## Recommendation

**Approach 1**, with one addition to capture Approach 2's single real advantage: gate
the `REROUTE_ONDEMAND` action behind a **platform-controlled env-var switch** the SDK
reads (off by default). Disabling it is a deploy-time config change across the fleet -
enough for the urgent case (kill rerouting when on-demand is itself constrained)
without moving the *decision* out of the SDK. (A live, per-cause toggle - a
`reroute_allowed` field in the recorder verdict - stays available as a later
enhancement if instant runtime control is ever needed.)

This keeps the recorder a clean fact store (P1), the action vocabulary owned by its
executor (no version skew), the SDK as the orchestrator (P2), and reuses the built
scaffold. It concedes only Approach 2's general "change any policy at runtime"
flexibility, which a uniform, stable policy does not need.

Routing policy (the cause → action map that ships in the SDK):

| Cause | Action | Why |
|---|---|---|
| `SPOT_RECLAIM` / `NODE_LOST` | `REROUTE_ONDEMAND` (if switch on) | On-demand won't be reclaimed - the retry can finish. |
| `OOM_KILLED` | `FAIL_FAST` (attributed) | Same limits on-demand → same OOM. Rerouting wastes budget; fix limits or the leak. |
| `NONE` / recorder down | `RETRY_SAME` | No verdict - keep today's behaviour. Fail-safe default. |

## Consequences

**Positive:**
- The SDK gains no new *success-path* dependency; the recorder is consulted only on
  the failure/reroute path, as a fail-safe activity (recorder down → `RETRY_SAME`).
- Every SDK-based app inherits identical routing, with no per-app configuration.
- No Kubernetes code enters the SDK (P3); the recorder stays a reusable fact store
  shared with AE.
- Attribution is fully delegated to AE (P4), including terminal failures the SDK
  could not attribute.
- Platform retains a fleet-wide reroute kill-switch (env var) without owning the
  decision.

**Negative:**
- The routing policy lives in the SDK, so changing the mapping needs an SDK release
  (mitigated: uniform, stable policy; runtime kill-switch for the urgent case).
- Routing carries a soft, failure-path, fail-safe dependency on the recorder; an
  outage disables smart rerouting (falls back to `RETRY_SAME`) until it recovers.
- Inert until the app runtime provisions the second on-demand TWD (its own task
  queue) and the routing env var is set; the SDK depends on but does not create that
  pool (ADR-0002, ADR-0009 topology).

## Implementation

Resolved decisions:

- **Reroute switch → env var.** A new SDK env var (e.g.
  `ATLAN_ENABLE_INFRA_FAILURE_ROUTING`), off by default, mirroring
  `ATLAN_ENABLE_INFRA_FAILURE_DIAGNOSIS`. Deploy-time config, no per-app override.
- **On-demand pool → provisioned by app runtime as a second TWD.** The app runtime
  stands up a second worker pool as its own Temporal Worker Deployment (TWD) pinned to
  on-demand nodes, on its own task queue. The SDK provisions nothing; it reads that
  queue name from an env var (injected like the existing Worker Deployment vars via
  the TWD controller / Downward API) and targets it in the reroute
  `execute_activity(task_queue=<on-demand queue>)`. Until that TWD and env var exist,
  routing is inert.
- **Reroute budget → the remaining budget, no separate counter.** Rerouting does not
  grant a fresh allowance. The SDK's workflow-side re-dispatch (as with eviction
  retry) redirects the *remaining* `max_attempts` onto the on-demand queue - only the
  queue for the remaining attempts changes, not the total budget. (Contrast the
  eviction loop's separate `WORKER_EVICTION_MAX_RETRIES`.)

## Related

- `docs/infra-failure-diagnosis.md` - the Phase-1 scaffold this ADR builds routing on.
- `docs/worker-eviction-and-sigterm.md` - the graceful SIGTERM counterpart; why abrupt
  kills surface as heartbeat timeouts.
- ADR-0002 (Per-App Workers), ADR-0009 (Separate Handler/Worker Deployments) - the
  one-app-one-queue topology a second on-demand pool must extend.
- ADR-0013 (Error Hierarchy and Failure Taxonomy) - the vocabulary AE's attribution
  maps onto.
