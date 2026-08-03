# ADR-0018: Temporal Nexus as the Single Handler-Invocation Plane

## Status

**Proposed** — pending: Nexus enablement verification on per-tenant Temporal
deployments, sync-operation timeout ceiling validation, and alignment with the
pod-connectivity "drop the app server" discussion and the Continuous Pre-flight
initiative.

## Context

### One capability, four invocation paths

App authors own exactly one piece of domain logic on this surface: the `Handler`
contract (`test_auth` / `preflight_check` / `fetch_metadata`) — a pure
question → answer function that receives resolved credentials, config, and a
deadline, and returns a verdict. It never enforces anything and imports no
transport.

Around that single contract, four invocation paths have accumulated, each built
for one caller and each implementing a different subset of the surrounding
plumbing:

| Path | Entry point | Where it lives |
| --- | --- | --- |
| HTTP routes | `POST /workflows/v1/{auth,check,metadata}` | `application_sdk/handler/service.py` |
| SDR workflows | `sdr:test_auth` / `sdr:preflight_check` / `sdr:fetch_metadata` | `application_sdk/execution/_temporal/sdr.py` |
| Injected pre-extraction gate | `{app}:preflight` activity, first step of every generated workflow | `application_sdk/execution/_temporal/preflight_gate.py`, dispatched from `application_sdk/app/base.py::_run_preflight_gate` |
| Synchronous SDR dispatch (PR #2914, in flight) | HTTP routes forking to `sdr:*` workflow dispatch when `agent_json` is present | branch `feat/sdr-sync-handler-dispatch` |

Each implements a different subset of the required plumbing:

| Concern | HTTP (`service.py`) | `sdr:*` workflows (`sdr.py`) | Injected gate (`preflight_gate.py`) | Sync SDR dispatch (#2914) |
| --- | --- | --- | --- | --- |
| Credential resolution | pre-resolved + agent-json lift | `agent_json` only (**no GUID path**) | GUID + agent + named refs | delegates to `sdr:*` |
| Per-entrypoint dispatch (`_discover_handler_fn`) | ✓ | ✗ (silent fallback to app-level handler) | ✗ (silent fallback) | ✗ |
| Enforced budget | ✗ (advisory `timeout_seconds`) | ✗ (env-const activity timeouts) | ✓ (per-app 5–300 s, clamped, cancellation at deadline) | ✗ |
| Source-vs-plumbing failure classification | ✗ | ✗ | ✓ (routed off `FailureCategory`) | partial (HTTP error mapping) |
| Queryable outcome event | ✗ | ✗ | ✓ (pinned contract string, `check_matrix`, `gate_classification`) | ✗ |
| Deployment (object-store) checks | ✗ | ✓ (hardcoded via `_append_object_store_checks`) | ✗ | inherited |
| Enforcement (hard/soft posture) | n/a | n/a | ✓ (baked at worker build) | n/a |

Every known defect on this surface is a cell in that table where one path lacks
what another already has:

- **Credential-GUID gap** — an SDR-path invocation carrying only a stored
  `credential_guid` (no `agent_json`) resolves nothing; the handler runs
  credential-less and reports a spurious auth failure.
- **Multi-entrypoint misdispatch** — an app that ships per-entrypoint
  `handler.py` modules gets them over HTTP but silently gets the app-level
  handler from the gate and the SDR workflows.
- **Invisible probes** — `sdr:preflight_check` emits no outcome event, so any
  consumer built on the gate's telemetry (connector-pulse) cannot see it.
- **No classification outside the gate** — a scheduled prober without
  source-vs-plumbing classification pages on secret-store blips and Dapr rate
  limits as if the customer's source were down.

### The four use cases

1. **Interactive check (cloud-hosted app)** — a user clicks "Test connection" /
   "Check" in the UI; Heracles calls the app. Low volume, human waiting,
   seconds-scale answer expected.
2. **Pre-extraction gate** — every generated workflow's `_run` dispatches
   `{app}:preflight` as its first activity (HYP-1883). Needs durability,
   per-app budget enforcement, and hard/soft posture — it runs *inside* an
   already-running workflow on the same worker.
3. **Interactive check (SDR app)** — same as (1), but the only process that can
   reach the source and the customer secret store is the SDR worker in the
   customer's infrastructure, reachable exclusively through its **outbound**
   Temporal gRPC long-poll. No inbound connectivity exists or may be added.
4. **Continuous Pre-flight** — workflow-health probes due workflows on a
   schedule (a few times a day, fleet-wide) to predict failures before the next
   scheduled run. Requirements: cheap at fleet scale, nothing in customer run
   history, never blocks anything, must distinguish "source not ready" from
   "our plumbing hiccuped".

### Why the workflow-per-call shape exists at all

A Temporal client cannot execute an activity directly — activities exist only
inside workflow executions. The `sdr:*` workflows are therefore a durable
envelope around what is semantically a single request/response RPC. Anatomy of
a representative production test run of `sdr:preflight_check`:

| Metric | Value |
| --- | --- |
| Wall-clock | 6.05 s |
| Handler work (`total_duration_ms`) | 397 ms |
| History events | 23 |
| State transitions | 15 |
| Workflow tasks | 4 (schedule/start/complete cycles) |
| Activity tasks | 3 |
| Orchestration overhead | **~93 %** |

Nobody wanted durability for that call; the workflow was the only way to get a
task onto the queue. Every path that copies this shape (interactive SDR today,
Continuous Pre-flight if built on it) inherits the same overhead plus a
workflow execution in the tenant's visibility store per probe.

### The deployment-topology question

ADR-0009 split every app into an always-on handler (FastAPI) deployment and a
scale-to-zero worker deployment, justified by "handlers need instant HTTP
responses". That premise is under active challenge (pod-connectivity thread:
*"could we just drop the app server entirely — does it serve any purpose
distinct from the worker, if they're both cold-started anyway?"*). Independently,
SDR already broke the premise structurally: for SDR apps the server cannot run
handler ops in-process (wrong network, wrong secret store) — #2914 turns it
into a dispatch broker that starts `sdr:*` workflows anyway, adding a hop
without adding capability.

## Decision

Adopt **Temporal Nexus** as the single caller-facing invocation plane for
handler operations, and retire the per-app FastAPI handler deployment.

### The shape

1. **Every worker registers one Nexus service** — `handler-ops`, with
   operations `test_auth`, `preflight_check`, `fetch_metadata` — bound to the
   same `Handler` instance the activities use. Registration is one constructor
   argument on the existing `Worker`; **no new process, port, or deployment
   anywhere**. Nexus tasks arrive as responses to the worker's existing
   **outbound** long-poll, exactly like activity tasks — the customer-side
   network posture is unchanged and remains pull-only.
2. **Fast checks run as synchronous Nexus operations**: the handler result
   returns inline. **No workflow execution is created, no event history, no
   visibility record, no run ID.**
3. **Slow checks escalate to asynchronous Nexus operations** backed by the
   existing `sdr:*` workflows, demoted to this single remaining role. The
   caller API is identical for both; the protocol carries the escalation.
4. **Callers invoke over the Temporal frontend's Nexus HTTP dispatch surface**
   — plain in-VPC HTTP, no Temporal client required — or natively from within
   a workflow (durable Nexus operation) where the caller is itself a workflow.
5. **The pre-extraction gate keeps its activity form.** It runs inside an
   already-running workflow on the same worker; converting it to Nexus would
   add a brokered hop for nothing. It shares the same underlying invocation
   core (below) so verdict semantics cannot drift.
6. **The invocation core is implemented once**, SDK-side, and shared by the
   Nexus service, the gate activity, and (during migration) the HTTP routes.

### Mechanics — what runs where

Nothing new spins up. The "server-shaped" responsibilities split across
components that already exist:

| Responsibility | Component | Status |
| --- | --- | --- |
| Routing (`endpoint → namespace + task queue`) + request/response brokering | Temporal frontend (per tenant) | already deployed; gains endpoint config *records*, not processes |
| Request handling (executing the handler) | the existing worker process, via its poll loop | one new constructor argument |
| Public API | Nexus HTTP dispatch on the frontend | replaces the per-app FastAPI service |

Worker-side registration (SDK-internal, illustrative):

```python
# inside create_worker() — the only registration change
worker = Worker(
    client,
    task_queue=task_queue,                                 # same queue as today
    workflows=[...],                                       # existing
    activities=[...],                                      # existing, incl. the gate
    nexus_service_handlers=[HandlerOpsService(handler)],   # ← the addition
)
```

`HandlerOpsService` is a thin class whose operations call the invocation core,
which calls the same `Handler` instance everything else uses — same process,
same event loop, same credential-store access as when an activity triggers it.

Caller-side invocation:

```text
# Any in-VPC HTTP caller (Heracles, workflow-health, tooling) — no Temporal client:
POST {temporal-frontend}/nexus/endpoints/{endpoint}/services/handler-ops/preflight_check
body: PreflightInput (JSON)          → 200 with PreflightOutput (sync path)
                                     → operation token (async escalation path)

# A workflow caller (e.g. workflow-health's sweep), durable form:
nexus_client = workflow.create_nexus_client(endpoint=..., service="handler-ops")
result = await nexus_client.execute_operation("preflight_check", input)
```

End-to-end flow for the SDR case:

```text
Heracles / caller (Atlan VPC)
        │  HTTP POST (in-VPC, authorizer-scoped)
        ▼
Temporal frontend (per tenant, already deployed, already mTLS)
        ▲  same outbound long-poll the agent already maintains
        │  (Nexus task delivered as a poll response — pull-only, no inbound)
SDR worker (customer infra) ──► invocation core ──► Handler.preflight_check()
        │
        ▼  result returns inline on the same request — no workflow anywhere
```

### The endpoint model

A Nexus *endpoint* is a cluster-scoped routing record mapping a name to a
target namespace + task queue:

- **Cloud-hosted app** → one endpoint per app, targeting the app's worker task
  queue (pool-aware for ADR-0016 apps).
- **SDR connection** → one endpoint per agent, targeting the agent's task
  queue (`atlan-{agent-name}` today). This **replaces** #2914's bespoke
  dispatch machinery: agent-json sniffing to detect SDR, string-derived queue
  names, and the pre-dispatch poller check all collapse into endpoint routing —
  placement becomes a property of the *connection's deployment*, recorded at
  provisioning time, instead of being inferred per-request from payload shape.

Endpoint provisioning is automated in tenant tooling wherever agents/apps are
registered today; an unreachable worker surfaces as a fast, typed Nexus
timeout — which *is* the "agent down" signal, without a pre-check.

### The invocation core (shared, SDK-side)

One implementation, consumed by the Nexus operations, the gate activity, and
the HTTP routes during migration:

- **Credential resolution for all routes** — GUID → local store → vault,
  agent-spec, and named refs — via `CredentialRef.resolve_or_none`. Requires
  the additive `credential_guid` + `extraction_method` fields on
  `AuthInput` / `PreflightInput` / `MetadataInput` (defaults `""`,
  backward-compatible), which also makes those inputs structurally satisfy the
  `CredentialResolvable` protocol. Callers pass references, never values;
  resolution happens only at the execution site.
- **Per-entrypoint dispatch** — `_discover_handler_fn` lifted out of
  `service.py` into the core, so multi-entrypoint apps resolve identically on
  every path.
- **Budget enforcement** — the gate's semantics generalized: budget net of
  credential resolution, stamped on `PreflightInput.timeout_seconds`,
  cancellation at the deadline, honest attribution when resolution consumes
  the budget.
- **Source-vs-plumbing classification** — the gate's `FailureCategory`-routed
  split (`source_unverifiable` vs `gate_broken`), so *every* caller can
  distinguish "the source is not ready" from "our plumbing broke".
- **Telemetry** — the pinned outcome event emitted from every path, extended
  with a `trigger` attribute: `interactive` / `extraction` / `continuous`.
  One queryable table serves the UI, the gate dashboard, and Continuous
  Pre-flight, and probe rows are separable from run-attributed rows.
- **Deployment (object-store) checks by caller intent** — selected via
  `checks_to_run` / trigger rather than hardcoded into one path.
- **Enforcement stays out of the core.** Verdict (app) / invocation (core) /
  enforcement (caller policy) are separate layers. The gate resolves its
  posture exactly as today (`ATLAN_PREFLIGHT_GATE_MODE` env >
  `App.preflight_gate_mode` > soft); Nexus callers **never enforce** — a
  probe or interactive check must never abort anything, and must not inherit
  a hard-mode posture.

### How each use case is served

| # | Use case | Path | Workflow created? | History footprint |
| --- | --- | --- | --- | --- |
| 1 | Interactive, cloud app | Heracles → Nexus HTTP → app worker → handler | **No** | none |
| 2 | Pre-extraction gate | unchanged: `{app}:preflight` activity inside the run | inside the existing run | in the run's own history, as today |
| 3 | Interactive, SDR app | Heracles → Nexus HTTP → endpoint routes to the agent's task queue → SDR worker → handler | **No** (sync) | none |
| 3b | Interactive, slow source | same call; async operation → backing workflow (`sdr:*`) | backing workflow only | on the backing workflow |
| 4 | Continuous Pre-flight | workflow-health's sweep → Nexus (HTTP or durable caller) with `trigger=continuous`, never enforcing | **No** | none — nothing in customer run history, nothing in visibility |

**"Is it the same across all implementations?"** Same handler, same contract,
same resolution, same classification, same telemetry — one invocation core.
Two invocation *forms* remain, honestly distinguished by where the caller
stands: outside a run → Nexus operation; inside the run → the gate activity.
Both are thin skins over the same core.

**"Is it invoked via an API? Does it run a workflow?"** Yes — a plain HTTP
call against the Temporal frontend (or a native Nexus call from a workflow).
It does **not** run a workflow on the fast path; a workflow appears only as
the async backing for a slow source, and inside extraction where a workflow
already exists.

### Retiring the app server

With Nexus HTTP as the API surface, the always-on FastAPI handler deployment
loses its remaining purpose. Each of its current responsibilities has a home:

| Server responsibility today | Home after this ADR |
| --- | --- |
| `/workflows/v1/{auth,check,metadata}` | Nexus operations on the worker |
| SDR dispatch broker (#2914) | superseded — endpoint routing replaces agent-json sniffing, queue derivation, and the poller pre-check |
| `/workflows/v1/start` (run triggering) | Temporal frontend HTTP API / existing orchestrator path (already Temporal-native) |
| Per-entrypoint dispatch (`_discover_handler_fn`) | the shared invocation core |
| Request normalization / compat shims (`_normalize_preflight_request`) | the invocation core's input boundary |
| Health endpoints | worker health server (`:8081`), as today |
| Handler auth manager, temporal-core metrics proxy | audit each: move to worker bootstrap or retire with the server (tracked as migration work, not silently dropped) |

Pod-budget note: deleting the always-on handler pod does **not** require
accepting cold-start latency for interactive checks. Apps with meaningful
interactive traffic set the worker's `minReplicas: 1` — the pod budget
previously spent on the handler moves to the worker. Net pods per app: same or
fewer; deployments per app: **one**. This is the answer to ADR-0009's
challenge rather than a repeal of its cost goals: scale-to-zero remains the
default for the long tail.

## Options Considered

### Option 1: Status quo — four bespoke paths

Rejected. The divergence matrix above *is* the defect list; every new caller
(Continuous Pre-flight is the fourth) adds another partial reimplementation.

### Option 2: FastAPI facade as the single public surface (#2914 extended)

The server forks internally: in-process for cloud apps, `sdr:*` workflow
dispatch for SDR. Callers stay HTTP-only and Temporal-free.

Pros: smallest delta from today; Heracles unchanged; the in-process path is
optimal for cloud apps (~400 ms, zero orchestration); handler pods are already
always-on, so probes cost no marginal infrastructure.
Cons: requires the always-on server that ADR-0009 is being challenged over;
keeps the workflow-per-call envelope (and its ~93 % overhead) for every SDR
invocation; keeps two transports forked inside the server; the dispatch layer
(queue derivation from agent-json, poller pre-check, pickup/job budget split)
is bespoke infrastructure that Nexus provides natively.

### Option 3: Workflow-per-call everywhere (extend `sdr:*`)

Rejected. Maximizes the measured orchestration overhead, pollutes visibility
with probe executions, still needs the credential/dispatch/classification gaps
closed, and still requires a Temporal client in every caller.

### Option 4: Actor pattern — long-lived per-agent workflow + synchronous Update

One always-running workflow per agent; callers send Updates as RPC.
Workable fallback if Nexus were unavailable: no per-probe execution, warm-path
latency comparable. Rejected while Nexus is available: re-implements RPC on
workflow machinery, accumulates history requiring continue-as-new churn, needs
per-agent singleton lifecycle management, and callers still need Update
plumbing.

### Option 5: New channel (NATS request-reply / reverse tunnel / WebSocket broker)

Rejected on the channel-selection rule: the customer already operates exactly
one authenticated, firewall-traversing, monitored channel — the worker's
outbound gRPC poll. A second channel duplicates security review, credential
management, HA, and monitoring on both ends to buy a capability Nexus provides
on the existing one. Never add a second hole; change the protocol on top of
the existing hole.

### Option 6 (chosen): Temporal Nexus

RPC semantics over the machinery already deployed: service/operation
registration on the worker (FastAPI-shaped developer experience, Temporal
substrate), frontend-brokered routing, sync ops with zero execution footprint,
async escalation built into the protocol, same mTLS, same task-queue
permission model.

Per-call cost comparison:

| Option | New infra | Per-call cost | Customer exposure |
| --- | --- | --- | --- |
| Workflow-per-call (today) | none | ~23 events, 4+ queue round trips, an execution record | none |
| **Nexus sync op** | endpoint records only | ~1 brokered round trip, **zero** history/executions | none — same poll |
| Actor workflow + Update | none | a few events appended + continue-as-new churn | none |
| New broker / tunnel | broker + creds + HA on both ends | ~ms | new egress to review |

## What Changes Where

| Layer | Change | App-author impact |
| --- | --- | --- |
| SDK | `create_worker` registers the `handler-ops` Nexus service bound to the existing handler; the shared invocation core is extracted (resolution, dispatch, budget, classification, events); additive `credential_guid` + `extraction_method` fields on `AuthInput` / `PreflightInput` / `MetadataInput`; `sdr:*` workflows re-scoped to async backing | **zero — inherited via version bump** |
| Platform / Temporal | enable Nexus on per-tenant deployments (dynamic config + HTTP dispatch surface); endpoint provisioning per app (cloud) and per agent (SDR), automated in tenant tooling; authorizer claim mapping for Nexus HTTP | zero |
| Heracles | base URL swap: app-server service → Temporal frontend Nexus URL; response mapping; retire per-app HTTP service discovery | zero |
| Helm / atlan monorepo | remove handler deployment + service from app subcharts; `minReplicas` knob on worker for interactive apps; KEDA scaler updated for Nexus task backlog | zero |
| workflow-health | consume Nexus with `trigger=continuous` | zero |
| Apps | none mandatory; optional per-app declarations (gate mode, budget) unchanged | **zero code** |

Fleet delivery: SDK release → renovate bump → conformance rule flags stragglers
→ remediation loop closes them. No per-app PRs.

### Migration sequence

1. **Verify preconditions** (below). Nothing else starts until these pass.
2. Extract the shared invocation core; rewire the gate activity and the HTTP
   routes onto it with behavior-preserving policies (pure refactor, lands
   independently and pays for itself even if Nexus stalls).
3. Register the Nexus service on workers behind a flag; provision endpoints on
   a staging tenant; prove the four use-case paths end-to-end.
4. Cut Heracles over per tenant; #2914's dispatch layer is bypassed (its
   agent-json lift and error-classification learnings fold into the core).
5. Continuous Pre-flight onboards as a Nexus consumer.
6. Remove handler deployments from subcharts; mark ADR-0009 superseded on the
   handler side.

## Preconditions and Risks

- **Sync-operation timeout ceiling.** Nexus sync calls are bounded per attempt
  (~10 s class by default; server dynamic config governs the cap). Fleet
  preflight p95 is under 14 s for every app except one federated source
  (~124 s). Mitigation: tune the cap for headroom on the common case; async
  escalation covers the tail by design. **Validate the cap on our server
  version before Phase 3.**
- **Nexus enablement + provisioning.** Server version (2.42.x) is well past
  Nexus GA and the pinned SDK stack ships support (`temporalio 1.30.0` with
  `nexus-rpc 1.4.0` in `uv.lock`); per-tenant dynamic config and endpoint
  provisioning automation must be built and verified.
- **Authorization.** Nexus HTTP dispatch must sit behind the Temporal
  authorizer with namespace-scoped claims, in-VPC only. Confirm claim-mapper
  coverage; the endpoint surface must not widen who can invoke handler ops
  relative to today's app-server network policy. Inputs carry credential
  *references*, never values — resolution stays at the execution site.
- **KEDA visibility.** Confirm the scaler sees Nexus task backlog (or pin
  `minReplicas: 1` for interactive apps). Irrelevant for SDR — agents are
  customer-run and always-on.
- **Payload limits.** Nexus payloads are bounded (~2 MB class); handler inputs
  and outputs are far below. `fetch_metadata` outputs are the one surface to
  spot-check.
- **Fake green.** A `DefaultHandler` app answers `READY` ("no preflight handler
  registered"). Callers that aggregate (Continuous Pre-flight) must key off the
  capability manifest / a typed `handler_present` field, not verdict alone —
  the same rule that already governs SDR registration (`has_real_handler` in
  `worker.py`).
- **Interactive latency for scale-to-zero cloud apps.** A cold worker adds
  KEDA wake-up to the first interactive check. Mitigation is the `minReplicas`
  knob per app; the trade is explicit and per-app rather than fleet-wide.
- **Transitional coexistence.** HTTP endpoints remain during migration; the
  facade and Nexus answer identically because both sit on the shared core.
  The compat window and its end date are part of Phase 4's definition of done.

## Open Questions

1. Exact Nexus request-timeout cap available on our server build, and whether
   it can be raised per endpoint rather than cluster-wide.
2. Endpoint granularity for multi-pool apps (ADR-0016): one endpoint per pool
   queue, or one endpoint with pool routing folded into the operation input.
3. Whether `/workflows/v1/start` consumers move to the Temporal frontend HTTP
   API directly or want a `start_run` Nexus operation for symmetry.
4. Where the handler auth manager's background credential refresh lands once
   the server process is gone.
5. Naming: whether the Nexus service adopts `{app}` namespacing like
   activities (`get_activity_name`) or stays a fixed `handler-ops` service
   behind per-app endpoints (current proposal: fixed service name, per-app
   endpoints — the endpoint is the namespace).

## Relationship to Prior ADRs

- **ADR-0001 (per-app handlers): unaffected.** The handler contract is the
  fixed point of this design.
- **ADR-0009 (separate handler/worker deployments): superseded for the handler
  side** once Phase 6 completes. Its cost rationale (scale-to-zero workers)
  survives; its always-on-server rationale is met by Nexus + per-app
  `minReplicas` instead of a second deployment.
- **ADR-0016 (multi-pool worker routing): compose.** Nexus endpoint → task
  queue routing must target the correct pool queue for apps using pools (see
  Open Questions).
