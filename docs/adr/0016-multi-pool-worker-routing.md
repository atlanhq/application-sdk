# ADR-0016: Multi-Pool Worker Routing — Apps Self-Route Activities to Differently-Scaled Task Queues

## Status

**Accepted** — ratified by discussion in BLDX-1375 / Slack #builder-experience 2026-06-04.
Implemented in application-sdk PR #2351 (BLDX-1342, BLDX-1375, BLDX-1382).

> **Supersedes preliminary ADR-0015 proposal** (PR #1991, never merged). This document
> carries the same decision and options analysis with naming and design updates applied
> during implementation: `profile` → `pool`; single `DeployConfig` block → `pools:
> Mapping<String, Pool>` map; codegen artifact replaced by env-var resolution + conformance
> rules K032/K033 (FND-5230). See §Design Divergences for details.
>
> **Correction (FND-5230):** earlier revisions named the build-time gate "P016
> PoolKeyMismatch". No such rule ever shipped (P016 is `EntryPointContractCodeDrift`),
> so until K032/K033 nothing checked `@task(pool=)` against the contract. The gate
> is K032 `TaskPoolNotDeployed` and K033 `UnusedWorkerPool`.

## Context

ADR-0002 established that each app runs on **one** worker bound to **one** Temporal task queue,
with KEDA scaling that worker to zero when the queue is empty.

The QI (Query Intelligence) app exposed a gap: its per-query memory needs are **bimodal**.
Roughly 90–95% of queries need <4 GB; a small fraction spike to ~17 GB due to large or complex SQL
inputs. Because today's model has a single worker pool, VPA must recommend the worst-case 17 GB for
**every** replica. The result is ~30% average memory utilisation — every worker carries 17 GB
allocations, while the actual average sits at ~3 GB. This is strictly worse than the old Argo
"retry the pod with 2× memory on OOM" behaviour.

Additionally, a third resource tier is emerging: QI can skip Gudusoft in favour of an in-house
lineage generator that needs ~700 MB — far below both existing buckets.

The team's stated preference is **proactive classification** (pattern-detect before dispatching)
over **reactive retry** (run, fail, escalate). The open question was whether the SDK abstraction
and Temporal primitives could support this.

### Temporal feasibility (confirmed)

Temporal fully supports routing activities to a different task queue than their workflow:

```python
await workflow.execute_activity(
    activity_name,
    args=[...],
    task_queue="other-queue",   # any worker polling this queue will pick it up
    start_to_close_timeout=...,
)
```

The worker polling `"other-queue"` must have that activity *registered* (over-registration of
activities that are never dispatched to a queue is harmless). One Worker entity = one task queue;
independent KEDA scaling therefore requires **separate K8s Deployments**, one per pool.

Temporal's official `worker_specific_task_queues` Python sample uses exactly this pattern.

### SDK feasibility (confirmed, change is small)

The routing seam in the SDK is one call in `_create_task_activity_wrapper`
(`application_sdk/app/base.py`):

```python
await execute_activity_with_eviction_retry(
    f"{app_name}:{task_name}",
    args=[task_context, input_data],
    start_to_close_timeout=...,
    # no task_queue= → activity runs on the workflow's own queue
)
```

`execute_activity_with_eviction_retry` already forwards `**kwargs` to `workflow.execute_activity`
unchanged, so threading `task_queue=` through is mechanical when a task has a declared pool.

The launch seam already exists too: `ATLAN_TASK_QUEUE` / `--task-queue` (`main.py`) lets a second
deployment of the **same image** bind a worker to a different queue. No new launch code is
needed — just a different environment variable.

The real gap was the **deployment descriptor**: `App.pkl`'s `DeployConfig` modelled exactly one
worker pool. A Python-only routing change with no matching pool deployment would dispatch
activities to an unstaffed queue (they would hang until `start_to_close_timeout`). Extending the
Pkl contract is therefore a **hard prerequisite**, sequenced first.

---

## Decision

We extend ADR-0002 from **one pool per app** to **N pools per app**, where N ≥ 1.

Each pool is a separately deployed worker bound to its own task queue, with its own KEDA policy
and resource profile. The app's Python code selects a pool by calling a **`@task`-decorated
method that is statically bound to that pool at decoration time** — never by passing a runtime
string.

---

## Options Considered

### Option 1: Static per-`@task` pool affinity — Chosen

Worker pools are declared in the Pkl contract (`App.pkl` `pools:` map). `@task(pool="heavy")`
pins an activity type to a named pool. The app's `run()` method (or a preprocessor `@task`)
calls the appropriate pool-pinned method based on its classification:

```python
class QIApp(App):

    @task                       # default pool (~4 GB)
    async def analyse_light(self, i: AnalyseInput) -> AnalyseOutput: ...

    @task(pool="heavy")         # heavy pool (~20 GB, KEDA-scaled-to-0)
    async def analyse_heavy(self, i: AnalyseInput) -> AnalyseOutput: ...

    @task(pool="lineage")       # lineage pool (~700 MB, KEDA-scaled-to-0)
    async def analyse_lineage(self, i: AnalyseInput) -> AnalyseOutput: ...

    async def run(self, i: BatchInput) -> BatchOutput:
        for q in i.queries:
            bucket = self._classify(q)   # pure preprocessor logic, no side effects
            if bucket == "heavy":
                await self.analyse_heavy(AnalyseInput(query=q))
            elif bucket == "lineage":
                await self.analyse_lineage(AnalyseInput(query=q))
            else:
                await self.analyse_light(AnalyseInput(query=q))
```

When processing between pools is genuinely identical, the `@task` method bodies are one-liners
that delegate to a shared utility function — **zero real duplication**:

```python
@task
async def analyse_light(self, i: AnalyseInput) -> AnalyseOutput:
    return await _run_analysis(i)    # shared impl

@task(pool="heavy")
async def analyse_heavy(self, i: AnalyseInput) -> AnalyseOutput:
    return await _run_analysis(i)    # same impl today; seam exists for future divergence
```

Keeping the `@task` methods distinct even when implementations are currently identical is
**deliberate**: it preserves the seam to introduce per-pool differences later (different
pre-processing, memory-mapped caches, batching parameters, instrumentation) without a breaking
change to callers.

The corresponding Pkl contract (the first key is the main worker, which runs `analyse_light`;
the dedicated pools come after it):

```pkl
pools {
  ["default"] = new Pool {
    keda { minReplicaCount = 1 }
  }
  ["heavy"] = new Pool {
    keda { minReplicaCount = 0; cooldownPeriod = 30 }
    resources = new ResourceConfig {
      requests { ["memory"] = "20Gi" }
      limits   { ["memory"] = "24Gi" }
    }
  }
  ["lineage"] = new Pool {
    keda { minReplicaCount = 0; cooldownPeriod = 30 }
    resources = new ResourceConfig {
      requests { ["memory"] = "700Mi" }
    }
  }
}
```

**Pool key alignment:** the string in `@task(pool="heavy")` **must exactly match** the key in
`pools { ["heavy"] = ... }`, and that key must not be the first one (see Design Principle 2).
This is enforced by conformance rule **K032 TaskPoolNotDeployed** — a blocking CI check, not a
warning.

**Pros:**
- Pool↔task binding is **known and validated at CI time** — a typo or missing pool key is caught
  before merge, not when an activity times out on an unstaffed queue (K032).
- Aligns with ADR-0004 (Build-Time Type Safety Over Runtime Validation).
- Routing is ordinary app branching — no new concepts for app developers.
- Queue name is resolved once at wrapper construction time (env var read); no repeated derivation.
- Backward compatible: apps with no pool declarations behave byte-identically to today.

**Cons:**
- N pools = N `@task` methods for the same logical operation (addressed by the delegation pattern
  above).

### Option 2: Dynamic per-call routing — Rejected

A per-invocation `pool=` override on the task wrapper: `await self.analyse(q, pool="heavy")`.
One task method; the caller picks the pool at call time.

**Rejected because:** the pool value is a runtime string — a typo or a pool removed from the
Pkl silently routes activities to an unstaffed queue (hung until timeout). This contradicts
ADR-0004's principle of catching errors as early as possible. Option 1 covers the same
capability with CI validation; anything Option 2 expresses, Option 1 expresses by calling a
different `@task` method. The marginal DRY gain does not justify reopening a runtime failure
surface.

### Option 3: Failure-driven escalation — Opt-in safety net only

Run on the default pool; on resource-exhaustion failure, the existing
`execute_activity_with_eviction_retry` loop re-dispatches the activity to a larger pool.

**Not the primary mechanism** because:
- `SIGKILL` (OOMKilled) gives no graceful signal; reliably attributing a failure to memory
  exhaustion vs. a bug requires heuristics.
- Wastes a full attempt on the default pool — the "try-and-fail" cost the team described as
  worse than the old Argo behaviour.
- Reactive, not proactive.

**However**, it is offered as an **opt-in complementary backstop** for mis-classified work. The
extension point is `execute_activity_with_eviction_retry` itself. An app opts in by declaring an
`escalation_pool` on the pool or task. This is tracked as a separate, lower-priority follow-up
ticket.

---

## Design Principles

### 1. Pool key is the shared identifier — contract and code use the same string

The pool key in `pools { ["hot"] = new Pool { ... } }` is the **exact same string** used in
`@task(pool="hot")` in Python. There is no intermediate name-mapping layer. This eliminates the
two-declaration problem: you cannot typo one side without K032 failing the CI build.

**Pool key format:** pool keys must be **lowercase kebab-case** (e.g. `"heavy"`, `"cold-tier"`).
This is enforced at decoration time by the `@task` validator and mirrors `App.pkl` `name` keys.
The `ATLAN_POOL_<POOL>_QUEUE` env-var key is derived by uppercasing the pool name **and
replacing hyphens with underscores** (`"cold-tier"` → `ATLAN_POOL_COLD_TIER_QUEUE`). Mixed or
upper-case pool keys are rejected at decoration time; the `-` → `_` normalization is handled
automatically by `resolve_pool_queue` in `application_sdk/app/registry.py`.

Queue names are resolved **at workflow-run time** — specifically when `_create_task_activity_wrapper`
is called from `_wrap_instance_tasks` inside `@workflow.run`. Env vars are read via
`os.environ.get(...)` at that point and captured in the wrapper closure.

**Rollout-drain contract:** because the resolved queue name is captured at workflow-run time
(not at worker startup), a Helm rollout that changes `ATLAN_POOL_<POOL>_QUEUE` or
`ATLAN_TASK_QUEUE` mid-flight can cause non-determinism if an in-progress workflow is replayed
on a pod with different env vars. The `task_queue=` kwarg recorded in Temporal history would
differ from the value computed on replay, and Temporal raises a non-determinism error. To avoid
this: **drain all running workflows before changing pool-related env vars** (standard
Single-Deployment rollout practice — ensure old pods are drained before new ones pick up work).
This matches the existing `on_complete` pattern (line 1282 of `base.py`) and is safe for the
common case where all pods in a cohort share identical env.

Queue name resolution order:

1. `ATLAN_POOL_<POOL>_QUEUE` — explicit override (Helm sets this if a custom queue name is needed).
2. `{ATLAN_TASK_QUEUE}-{pool}` — derived from the app's base queue (default, requires no extra config).

The derived default ensures that two different apps that both declare `pool="heavy"` automatically
get different Temporal queues (`qi-app-queue-heavy` vs `other-app-queue-heavy`), because
`ATLAN_TASK_QUEUE` is already per-app. Python code never contains a queue name string; the seam
between contract (Pkl) and deployment (Helm) is `ATLAN_TASK_QUEUE`, which is already set
unconditionally for every deployment.

### 2. CI validation (K032/K033) as the build-time gate

`@task(pool=...)` names are validated against the `deploy.pools` keys of the contract (or the
`pools:` mirror in `atlan.yaml` when the contract declares none) by two conformance rules:

- **K032 `TaskPoolNotDeployed` (BLOCK):** a `@task(pool="x")` names no dedicated worker pool.
- **K033 `UnusedWorkerPool` (WARN):** a dedicated pool has no `@task(pool=...)` in the Python
  source. WARN, because an app can route to a pool by means a static check cannot see.

The contract-toolkit makes the **first** `pools` key the app's main worker. That worker polls the
base queue `atlan-<app>-<deployment>`, not `atlan-<app>-<deployment>-<key>`. Only keys after the
first render a dedicated worker pool. So K032 also fails a `pool=` that names the first key, and
K033 never flags it. A `deploy.workerPools[].name` in a hand-written `atlan.yaml`, or a pool with
an explicit `ATLAN_POOL_<POOL>_QUEUE`, also counts as routable.

This replaces the earlier proposal (PR #1991) of a generated `worker_profiles.py` artifact.
The conformance-rule approach achieves the same build-time safety without adding a generated file
to the app's source tree or requiring the SDK to import it at startup.

### 3. Startup observability

Each pool worker's resolved queue name is logged at startup via the `worker_start` lifecycle
event. A misconfigured deployment (wrong env var) is diagnosable in seconds rather than
manifesting as a silent activity backlog.

### 4. Default path is byte-identical to today

A task with no `pool` declaration generates a wrapper that calls
`execute_activity_with_eviction_retry` with **no** `task_queue` kwarg — exactly today's
behaviour. Apps with no pool declarations compile, test, and run without any change.

---

## Design Divergences from Preliminary ADR (PR #1991)

| Preliminary (PR #1991) | Implemented (this ADR) | Rationale |
|---|---|---|
| `@task(profile="heavy")` | `@task(pool="heavy")` | `pool` is self-describing; `profile` was ambiguous |
| `DeployConfig` + a list of extra pools | `pools: Mapping<String, Pool>` | Explicit map; pool key IS the `@task` pool name; no separate `profiles:` indirection |
| `worker_profiles.py` generated artifact | `ATLAN_POOL_<POOL>_QUEUE` env vars + K032/K033 conformance rules | Simpler; no generated file in app tree; same build-time safety via CI |
| Profile validated at App registration (runtime) | K032 validated at CI (pre-merge) | Earlier catch; no startup cost |
| `DeployConfig.profiles: Listing<String>` field | Dropped — pool key = task pool name | Eliminates the indirection; 1:1 mapping is always the common case |

---

## Consequences

### Positive

- Heavy work no longer inflates the resource floor for the 90%+ common case.
- Each pool is independently KEDA-scaled to zero — heavy pools cost nothing when idle.
- App developers get a simple, typed API (`@task(pool="heavy")`) with no new concepts.
- Pool↔task binding errors surface at CI, not at queue depth / timeout (K032).
- Backward compatible: zero changes to any existing app, test, or deployment that does not opt in.

### Negative

- More K8s Deployments per app (mitigated by the Helm chart, same trade-off as ADR-0002).
- The Pkl `pools:` schema extension and the out-of-repo Helm chart update are a **hard
  prerequisite** for any routing to be safe. Routing Python code landed simultaneously with
  the Pkl changes in PR #2351; Helm support must land before any app declares a non-default pool.
- The preprocessor / classifier adds a computation hop per unit of work. For QI this is
  negligible compared to a failed full attempt, but it is not free.

---

## Implementation

Implemented in **PR #2351** (application-sdk), which covers:

- **Phases 1–2** (BLDX-1342, BLDX-1382): `marketplaceCard` route/card split;
  `Deployment.pkl` → `Pool.pkl` source-module split.
- **Phase 3** (BLDX-1375): `pools: Mapping<String, Pool>` in `App.pkl`;
  `KedaConfig.cooldownPeriod`; `Pool` class.
- **Phase 4** (BLDX-1375): `@task(pool=...)` decorator; `ATLAN_POOL_<POOL>_QUEUE`
  resolution in `_create_task_activity_wrapper`.

- **Phase 5** (ARUN-1041): wired the contract to the chart. The Helm prerequisite
  below had landed out-of-repo under a different shape than this ADR assumed —
  `deploy.workerPools`, a **list** keyed by `name` with a **flat** `keda` block,
  rather than the contract's `pools:` mapping with `keda.temporal.targetQueueSize`.
  Nothing read `pools:`. Consequences, all now closed:
  - Pools after the first were emitted only into the ignored `pools:` block, so a
    second declared pool was **never staffed** — the failure this ADR set out to
    prevent. `App.pkl` now renders every pool after the first as a
    `deploy.workerPools[]` entry and owns the mapping→list and
    nested→flat-`keda` translation.
  - `resolve_pool_queue()` read `ATLAN_TASK_QUEUE`, which the chart does not set,
    so on a chart deployment every `@task(pool=...)` resolved to `None` and its
    activities silently ran on the workflow's default queue. It now falls back to
    `task_queue_from_env()`, reproducing the chart's own
    `atlan-<app>-<deploymentName>-<pool>` byte-for-byte. No `taskQueue` is
    generated: the pool key stays the single source, derived independently and
    identically on both sides.
  - `Pool.vpa` (typed `VpaConfig`) and `KedaConfig.maxReplicaCount` added, so the
    per-pool knobs CI validates are expressible in the contract instead of
    reached for through the untyped `overrides` escape hatch.

Tracking:
- **FND-5230** — K032 `TaskPoolNotDeployed` / K033 `UnusedWorkerPool` conformance rules
  (implemented; BLDX-1485 had named this gate "P016 PoolKeyMismatch", which never shipped).
- **ARUN-1041** — chart does not yet honor `workerPools[].env`, `envOverrides`, or
  `replicaCount`; the toolkit emits them so the gap is visible rather than
  silently dropped. Also open: `capacityType`,
  `maxConcurrentActivities`, and `terminationGracePeriodSeconds` are chart-side
  pool knobs with no typed contract field yet, and app-level `vpa` /
  `workerResources` still reach `atlan.yaml` only via `deploy.overrides`.
  When the chart sets a pool's `terminationGracePeriodSeconds`, it should
  also set `ATLAN_SHUTDOWN_GRACE_SECONDS` on that pool's pods to the same
  value. The SDK derives that pool's drain, post-drain and telemetry
  shutdown bounds from it (FND-4645). Without it the fixed defaults apply,
  whatever the pool's grace.
- Option 3 escalation backstop — separate lower-priority follow-up.

### Amendment (2026-11-24): `ATLAN_TASK_QUEUE` is derived, not set

The Decision above calls `ATLAN_TASK_QUEUE` "the seam between contract (Pkl) and
deployment (Helm)". For a deployed app, read that seam as the app and deployment
names instead. When `ATLAN_TASK_QUEUE` is unset, the SDK derives the base queue
as `atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}`
(`application_sdk.common.task_queue`), and the chart appends `-od` to the
deployment name for the on-demand worker, so each variant polls its own queue.

An app must not pin `ATLAN_TASK_QUEUE` in its contract's deploy or pool `env`
(or in `atlan.yaml`). A pin overrides the derivation for both variants, so the
`-od` worker polls the base queue and retries the activity rerouter moves to the
`-od` queue are never picked up. Pool queues, `{ATLAN_TASK_QUEUE}-{pool}`,
inherit the pin. Conformance rule K031 `PinnedWorkerTaskQueue` flags it. The
variable stays available as an override for local and CLI runs.

---

## Related ADRs

- **ADR-0002** — this ADR generalises from one pool per app to N pools per app.
- **ADR-0004** — build-time type safety; Option 1 chosen specifically to honour this principle.
- **ADR-0009** — separate handler and worker deployments; multi-pool workers are additional
  worker deployments on the same axis.
