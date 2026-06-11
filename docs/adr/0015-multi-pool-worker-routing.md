# ADR-0015: Multi-Pool Worker Routing — Apps Self-Route Activities to Differently-Scaled Task Queues

## Status

**Proposed** — ratified by discussion in BLDX-1375 / Slack #builder-experience 2026-06-04.
Implementation sequenced across follow-up tickets (see Consequences).

## Context

ADR-0002 established that each app runs on **one** worker bound to **one** Temporal task queue, with
KEDA scaling that worker to zero when the queue is empty.

The QI (Query Intelligence) app exposed a gap: its per-query memory needs are **bimodal**.
Roughly 90–95% of queries need <4 GB; a small fraction spike to ~17 GB due to large or complex SQL
inputs. Because today's model has a single worker pool, VPA must recommend the worst-case 17 GB for
**every** replica. The result is ~30% average memory utilisation — every worker carries 17 GB
allocations, while the actual average sits at ~3 GB. This is strictly worse than the old Argo
"retry the pod with 2× memory on OOM" behaviour.

Additionally, a third resource tier is emerging: QI can skip Gudusoft in favour of an in-house
lineage generator that needs ~700 MB — far below both existing buckets.

The team's stated preference (from the thread and confirmed in this ADR) is **proactive
classification** (pattern-detect before dispatching) over **reactive retry** (run, fail, escalate).
The open question was whether the SDK abstraction and Temporal primitives could support this.

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

Temporal's official `worker_specific_task_queues` Python sample uses exactly this pattern: a shared
queue hosts the workflow and a router activity; the router returns the target queue name; subsequent
activities are dispatched there.

### SDK feasibility (confirmed, change is small)

The routing seam in the SDK today is one call in
`_create_task_activity_wrapper` (`application_sdk/app/base.py`):

```python
await execute_activity_with_eviction_retry(
    f"{app_name}:{task_name}",
    args=[task_context, input_data],
    start_to_close_timeout=...,
    # no task_queue= → activity runs on the workflow's own queue
)
```

`execute_activity_with_eviction_retry` already forwards `**kwargs` to `workflow.execute_activity`
unchanged, so threading `task_queue=` through is mechanical when a task has a declared profile.

The launch seam already exists too: `ATLAN_TASK_QUEUE` / `--task-queue` (`main.py`)
lets a second deployment of the **same image** bind a worker to a different queue. No new launch
code is needed — just a different environment variable.

The real gap is the **deployment descriptor**: `contract-toolkit/src/App.pkl`'s `DeployConfig`
models exactly one worker pool (one set of `resources`, one `keda` config). The Helm chart that
turns `atlan.yaml` into K8s Deployments + KEDA ScaledObjects lives in a separate platform repo.
A Python-only routing change with no matching pool deployment would dispatch activities to an
unstaffed queue — they would hang until `start_to_close_timeout`. The Pkl + Helm work is therefore
a **hard prerequisite**, sequenced first.

---

## Decision

We extend ADR-0002 from **one pool per app** to **N pools per app**, where N ≥ 1.

Each pool is a separately deployed worker bound to its own task queue, with its own KEDA policy
and resource profile. The app's Python code selects a pool by calling a **`@task`-decorated method
that is statically bound to that pool at decoration time** — never by passing a runtime string.

The three mechanisms considered are:

---

## Options Considered

### Option 1: Static per-`@task` affinity — Chosen

Worker pools are declared in the Pkl contract (`DeployConfig`). A codegen step emits a **read-only,
never-hand-edited** Python artifact listing profile names and their resolved queue names. The App
imports this artifact at registration time. `@task(profile="heavy")` pins an activity type to a
named pool, validated at registration against the generated profile list. The app's `run()` method
(or a preprocessor `@task`) calls the appropriate profile-pinned method based on its classification:

```python
class QIApp(App):
    # profiles auto-registered from generated artifact — never hand-declared here

    @task                        # default pool (~4 GB)
    async def analyse_light(self, i: AnalyseInput) -> AnalyseOutput: ...

    @task(profile="heavy")       # heavy pool (~20 GB, KEDA-scaled-to-0)
    async def analyse_heavy(self, i: AnalyseInput) -> AnalyseOutput: ...

    @task(profile="lineage")     # lineage pool (~700 MB, KEDA-scaled-to-0)
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

When processing between profiles is genuinely identical, the `@task` method bodies are
one-liners that delegate to a shared utility function — **zero real duplication**:

```python
@task
async def analyse_light(self, i: AnalyseInput) -> AnalyseOutput:
    return await _run_analysis(i)    # shared impl

@task(profile="heavy")
async def analyse_heavy(self, i: AnalyseInput) -> AnalyseOutput:
    return await _run_analysis(i)    # same impl today; seam exists for future divergence
```

Keeping the `@task` methods distinct even when implementations are currently identical is
**deliberate**: it preserves the seam to introduce per-profile differences later (different
pre-processing, memory-mapped caches, batching parameters, instrumentation) without a breaking
change to callers. The ADR author note: treating apparent duplication as a "con" mistakes a
structural affordance for a cost.

**Pros:**
- Profile↔task binding is **known and validated at build / registration time** — a typo or
  missing profile is caught before the first workflow run, not when an activity times out on a
  dead queue.
- Aligns with ADR-0004 (Build-Time Type Safety Over Runtime Validation).
- Routing is ordinary app branching — no new concepts for app developers.
- Queue name is resolved once and baked into the wrapper; no repeated derivation.
- Backward compatible: apps with no profile declarations behave byte-identically to today.

**Cons:**
- N pools = N `@task` methods for the same logical operation (addressed by the delegation pattern
  above).

### Option 2: Dynamic per-call routing — Rejected

A per-invocation `profile=` override on the task wrapper: `await self.analyse(q, profile="heavy")`.
One task method; the caller picks the pool at runtime.

**Rejected because:** the pool value is a runtime string — a typo or a profile removed from the
Pkl silently routes activities to a dead queue (hung until timeout). This contradicts ADR-0004's
principle of catching errors as early as possible. Option 1 covers the same capability with
build-time validation; anything Option 2 expresses, Option 1 expresses by calling a different
`@task` method. The marginal DRY gain does not justify reopening a runtime failure surface.

### Option 3: Failure-driven escalation — Opt-in safety net only

Run on the default pool; on resource-exhaustion failure, the existing
`execute_activity_with_eviction_retry` loop re-dispatches the activity to a bigger pool.

**Not the primary mechanism** for these reasons:
- `SIGKILL` (OOMKilled) gives no graceful signal; reliably attributing a failure to memory
  exhaustion vs. a bug requires heuristics.
- Wastes a full attempt on the default pool — exactly the "try-and-fail" cost the team described
  as worse than the old Argo behaviour.
- Reactive, not proactive — the thread explicitly converged on proactive classification.

**However**, it is offered as an **opt-in complementary backstop** for mis-classified work: edge
cases the classifier does not yet recognise. The extension point is `execute_activity_with_eviction_retry`
itself, which already handles eviction; escalation-on-OOM follows the same loop structure. An app
opts in by declaring an `escalation_profile` on the pool or task. This is tracked as a separate,
lower-priority follow-up ticket.

---

## Design Principles

### 1. One source of truth — Pkl contract, generated artifact, never two declarations

Worker pools (name + resources + KEDA config) are declared **once** in the Pkl `DeployConfig`
block. A codegen step (extending the existing `app/generated/` pipeline) emits a **read-only**
`worker_profiles.py` artifact — the contract side committing to specific profile names and
their resolved queue names. The App imports this artifact at registration time.

Manual edits to `worker_profiles.py` are **forbidden** (enforced by the same "do not edit"
header convention used by other generated files under `app/generated/`). Regeneration is a
no-op when the Pkl is unchanged, enabling drift detection in CI.

This removes two independent derivation sites and the associated mismatch risk:
- Queue names are not independently inferred from `workflow.info().task_queue` at runtime
  *and* from `_derive_task_queue` (which has three env-variable-dependent shapes) at launch.
- The Pkl contract is the **single** source; the generated artifact and `ATLAN_TASK_QUEUE`
  on the heavy pool's deployment are both produced from it by tooling, not by a human
  independently configuring two places to agree.

### 2. Build-time validation

Profile names declared in `@task(profile=...)` are validated against the generated profile
registry at App registration time (import time). An unknown profile → startup error, not a
hung activity.

### 3. Startup observability

Each pool's resolved queue name is logged at worker startup and exposed on the health endpoint.
This makes a misconfigured deployment diagnosable in seconds rather than manifesting as a
silent activity backlog.

### 4. Default path is byte-identical to today

A task with no `profile` declaration generates a wrapper that calls
`execute_activity_with_eviction_retry` with **no** `task_queue` kwarg — exactly today's
behaviour. Apps with no pool declarations compile and run without any change.

---

## Rationale

**Why extend ADR-0002 rather than add a new abstraction?**
The per-pool model is identical to the per-app model in ADR-0002: each pool is a separate K8s
Deployment + KEDA ScaledObject, listening on a distinct task queue, independently scaled. The
only new concept is *which pool, within a single app's image, handles which activity type*. This
is a direct generalisation of "one app, one pool" to "one app, N pools" — not a new pattern.

**Why not use Temporal's `Worker Sessions` API?**
Sessions provide host affinity (all activities in a session run on the same worker pod). They do
not provide resource-tiered pools and do not integrate with KEDA scaling. They address file-system
locality, not the bimodal memory problem.

**Why proactive classification over reactive escalation?**
Proactive classification (Option 1) avoids the wasted first attempt on an under-resourced worker.
For QI specifically, the team believes query-shape heuristics can pre-detect heavy work — the
Slack discussion confirmed this is "doable" and "orders of magnitude less" expensive than running
and failing. Reactive escalation (Option 3) is kept as a complementary backstop, not a substitute.

---

## Consequences

### Positive

- Heavy work no longer inflates the resource floor for the 90%+ common case.
- Each pool is independently KEDA-scaled to zero — the heavy pool costs nothing when no heavy
  work is queued.
- App developers gain a simple, typed API (`@task(profile="heavy")`) with no new concepts beyond
  what they already know.
- Profile↔task binding errors surface at startup, not at queue depth / timeout.
- Backward compatible: zero changes to any existing app, test, or deployment that does not opt in.

### Negative

- More K8s Deployments per app (mitigated by the Helm chart, same as ADR-0002's trade-off).
- The Pkl `DeployConfig` schema extension and the out-of-repo Helm chart update are a **hard
  prerequisite** for any routing changes to be safe. Routing Python code must not land until
  the deployment topology is in place.
- The preprocessor / classifier adds a computation hop per unit of work. For QI, this is
  "orders of magnitude less" than a failed full attempt, but it is not free.

---

## Implementation Sequence

This ADR describes the architecture. Implementation is split into sequenced follow-up tickets:

### Step 1 (prerequisite — must land before Step 2)

- Extend `contract-toolkit/src/App.pkl` `DeployConfig` (`:1272`) to model a *list* of named
  additional worker pools: each pool has a `name`, `resources`, and `keda` block.
- Extend `renderDeployConfig` (`:2519`) to emit each additional pool into `atlan.yaml`.
- Validate the new `atlan.yaml` shape passes `.github/scripts/validate_atlan_yaml.py`.
- Extend the out-of-repo Helm chart to emit a K8s Deployment + KEDA ScaledObject per pool,
  with `ATLAN_TASK_QUEUE` set to the pool's resolved queue name.
- Extend the existing `app/generated/` codegen pipeline to emit the read-only
  `worker_profiles.py` artifact from the Pkl contract.

### Step 2 (SDK seam — after Step 1 is deployed)

- Add optional `profile: str | None = None` to `TaskMetadata` (`application_sdk/app/task.py`)
  and to the `@task` decorator.
- At `App` registration time, auto-import `worker_profiles.py` and validate all
  `@task(profile=...)` names against it.
- In `_create_task_activity_wrapper` (`application_sdk/app/base.py`), resolve the task's
  declared profile → queue name **once at wrap time** and pass `task_queue=<queue>` into
  `execute_activity_with_eviction_retry` only when a profile is declared. No-profile tasks are
  unchanged.
- The generated wrapper call signature stays `wrapper(input_data)` — no per-call override.
- Confirm pushgateway grouping key is distinct across pools to avoid metric collision between
  the default pool's workers and the heavy pool's workers.
- Add test cases: default path emits no `task_queue`; profiled path emits the resolved queue;
  unknown profile fails at registration.

### Step 3 (optional — lower priority)

- Opt-in failure-driven escalation: extend `execute_activity_with_eviction_retry` to re-dispatch
  to a declared `escalation_profile` on resource-exhaustion failure.

---

## Related ADRs

- **ADR-0002** — this ADR generalises from one pool per app to N pools per app.
- **ADR-0004** — build-time type safety; Option 1 chosen specifically to honour this principle.
- **ADR-0009** — separate handler and worker deployments; multi-pool workers are additional
  worker deployments on the same axis.
