# ADR-0016: Multi-Pool Worker Routing — Apps Self-Route Activities to Differently-Scaled Task Queues

## Status

**Accepted** — ratified by discussion in BLDX-1375 / Slack #builder-experience 2026-06-04.
Implemented in application-sdk PR #2351 (BLDX-1342, BLDX-1375, BLDX-1382).

> **Supersedes preliminary ADR-0015 proposal** (PR #1991, never merged). This document
> carries the same decision and options analysis with naming and design updates applied
> during implementation: `profile` → `pool`; single `DeployConfig` block → `pools:
> Mapping<String, Pool>` map; codegen artifact replaced by env-var resolution + conformance
> rule P016 (BLDX-1485). See §Design Divergences for details.

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

The corresponding Pkl contract:

```pkl
pools {
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
`pools { ["heavy"] = ... }`. This is enforced by conformance rule **P016 PoolKeyMismatch**
(BLDX-1485) — a blocking CI check, not a warning.

**Pros:**
- Pool↔task binding is **known and validated at CI time** — a typo or missing pool key is caught
  before merge, not when an activity times out on an unstaffed queue (P016).
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
two-declaration problem: you cannot typo one side without P016 failing the CI build.

Queue names are resolved from `ATLAN_POOL_<POOL>_QUEUE` env vars at worker-wrapper construction
time — set by the Helm chart that deploys each pool. Python code never contains a queue name
string; the env var is the seam between contract (Pkl) and deployment (Helm).

### 2. CI validation (P016) as the build-time gate

`@task(pool=...)` names are validated against the `pools:` keys in `atlan.yaml` by conformance
rule P016 `PoolKeyMismatch` (BLDX-1485). Two sub-checks, both ERROR severity:

- **P016a:** a `@task(pool="x")` references a pool not declared in `atlan.yaml`
- **P016b:** a `pools:` entry has no corresponding `@task(pool=...)` in the Python source

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
| `worker_profiles.py` generated artifact | `ATLAN_POOL_<POOL>_QUEUE` env vars + P016 conformance rule | Simpler; no generated file in app tree; same build-time safety via CI |
| Profile validated at App registration (runtime) | P016 validated at CI (pre-merge) | Earlier catch; no startup cost |
| `DeployConfig.profiles: Listing<String>` field | Dropped — pool key = task pool name | Eliminates the indirection; 1:1 mapping is always the common case |

---

## Consequences

### Positive

- Heavy work no longer inflates the resource floor for the 90%+ common case.
- Each pool is independently KEDA-scaled to zero — heavy pools cost nothing when idle.
- App developers get a simple, typed API (`@task(pool="heavy")`) with no new concepts.
- Pool↔task binding errors surface at CI, not at queue depth / timeout (P016).
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

Tracking:
- **BLDX-1485** — P016 `PoolKeyMismatch` conformance rule (not yet implemented).
- Helm chart update (out-of-repo) — hard prerequisite before any production pool is staffed.
- Option 3 escalation backstop — separate lower-priority follow-up.

---

## Related ADRs

- **ADR-0002** — this ADR generalises from one pool per app to N pools per app.
- **ADR-0004** — build-time type safety; Option 1 chosen specifically to honour this principle.
- **ADR-0009** — separate handler and worker deployments; multi-pool workers are additional
  worker deployments on the same axis.
