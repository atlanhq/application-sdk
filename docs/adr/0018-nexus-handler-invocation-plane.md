# ADR-0018: Temporal Nexus as the Single Handler-Invocation Plane (Workflow-Free)

## Status

**Proposed** — gated on the Phase 0 spike (custom async operation handlers with
manual completion in the Python SDK; callback reachability from customer
infra; Nexus enablement on per-tenant Temporal deployments). Intended as the
design input for the pending sync between the SDR (#2914), Continuous
Pre-flight, and pod-connectivity ("drop the app server") discussions.

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

**Duration envelope:** handler checks range from sub-second (typical; a
representative production run measured 397 ms of handler work) through a fleet
p95 under 14 s per app, up to a hard ceiling of **~5 minutes** for slow
federated sources (the gate's own budget ceiling is 300 s). The design must
serve the entire envelope.

### Design principle: workflows are for durability, not transport

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

The per-call overhead is only half the cost. Every workflow on the handler
path is a *managed artifact*: it must be authored under replay-determinism
constraints, versioned across deploys, registered on every worker, sized with
timeout/retry pairs that have already churned twice in one week on #2914,
kept out of visibility/dashboard queries that mean "real runs", and reasoned
about during every incident. That management overhead recurs fleet-wide and
forever. Handler checks are **idempotent, read-only probes** — they gain
nothing from durable execution semantics and pay full price for them.

This ADR therefore adopts a hard rule: **the handler-invocation plane
schedules no workflows.** Workflows remain where durability genuinely earns
its cost — extraction runs (which already exist; the gate rides inside them,
scheduling nothing new).

### The deployment-topology question

ADR-0009 split every app into an always-on handler (FastAPI) deployment and a
scale-to-zero worker deployment, justified by "handlers need instant HTTP
responses". That premise is under active challenge (pod-connectivity thread:
*"could we just drop the app server entirely — does it serve any purpose
distinct from the worker, if they're both cold-started anyway?"*). Independently,
SDR already broke the premise structurally: for SDR apps the server cannot run
handler ops in-process (wrong network, wrong secret store) — #2914 turns it
into a dispatch broker that starts `sdr:*` workflows anyway, adding a hop
without adding capability. This ADR retires the app server; its
responsibilities are relocated below.

## Decision

Adopt **Temporal Nexus** as the single caller-facing invocation plane for
handler operations — **with no workflow anywhere on the path** — and retire
the per-app FastAPI handler deployment.

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
   visibility record, no run ID.** The sync window is raised from the 10 s
   default to ~20–30 s via server dynamic config
   (`component.nexusoperations.request.timeout`; self-hosted deployments may
   tune it — Temporal Cloud pins it), covering the fleet p95 with headroom.
3. **Slow checks (up to the 300 s envelope) run as asynchronous Nexus
   operations with in-process completion — no backing workflow.** The
   operation handler starts the check as a plain in-process task, immediately
   returns an async operation token, and when the check finishes the worker
   delivers the result via the **Nexus completion callback** — an HTTP POST to
   the callback URL that arrived with the start request. This manual-completion
   form is part of the nexus-rpc protocol (caller-specified callback URLs;
   completion-delivery helpers such as the Go SDK's
   `NewCompletionHTTPRequest`; a `CompletionHandler` interface on the caller
   side); workflow backing is Temporal's *convenience* pattern, not a
   protocol requirement. The callback is **outbound HTTPS from the worker** —
   the same direction as its existing long-poll; customer infra still accepts
   no inbound connections.
4. **Callers invoke over the Temporal frontend's Nexus HTTP dispatch surface**
   — plain in-VPC HTTP, no Temporal client required — or natively from within
   a workflow they already own (durable Nexus operation call), where async
   completion is wired into the caller's machinery automatically.
5. **The pre-extraction gate keeps its activity form.** It runs inside an
   already-running extraction workflow on the same worker — it schedules no
   new workflow, so it already satisfies the workflow-free rule. It shares the
   same underlying invocation core (below) so verdict semantics cannot drift.
6. **The `sdr:*` workflows are deleted at the end of migration**, not demoted:
   with manual completion covering the slow tail, no role remains for them.
   #2914's dispatch layer is superseded; its agent-json-lift and
   error-classification learnings fold into the invocation core.

### Mechanics — what runs where

Nothing new spins up. The "server-shaped" responsibilities split across
components that already exist:

| Responsibility | Component | Status |
| --- | --- | --- |
| Routing (`endpoint → namespace + task queue`) + request/response brokering | Temporal frontend (per tenant) | already deployed; gains endpoint config *records*, not processes |
| Request handling (executing the handler) | the existing worker process, via its poll loop | one new constructor argument |
| Slow-check state | an in-process operation registry on the worker (token → running task) | plain code, no infrastructure |
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

The two tiers, end to end (SDR case shown; cloud apps are identical minus the
customer boundary):

```text
── Fast tier (fits the ~20–30 s sync window) ──────────────────────────────────
Caller (Atlan VPC) ── HTTP POST ──► Temporal frontend
                                        ▲ outbound long-poll (existing)
                                        │ Nexus task delivered as poll response
                              SDR worker ► invocation core ► Handler.preflight_check()
Result returns inline on the same request. No workflow, no history, no run ID.

── Slow tier (up to ~300 s) — async, manual completion, still no workflow ────
Caller ── HTTP POST (carries a callback URL) ──► frontend ──► worker
worker: starts the check as an in-process task, returns an operation token
        immediately (the Nexus task slot is released — nothing is held open)
… check runs up to its budget …
worker ── outbound HTTPS POST (Nexus completion) ──► callback URL
   • Temporal-workflow caller: callback targets the caller's own cluster;
     its Nexus machinery wakes the waiting workflow automatically.
   • Plain HTTP caller (Heracles): callback targets the caller-supplied
     in-VPC endpoint (protocol-native; Atlan-side inbound is permitted).
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
(during migration) the HTTP routes:

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
  the budget. The async tier reuses the app's declared gate budget
  (`preflight_gate_timeout_seconds`, clamped 5–300 s) as its check budget, so
  one declared number governs every invocation form.
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

### Tier selection and the duration envelope

| Tier | Covers | Mechanism | Footprint |
| --- | --- | --- | --- |
| Sync | the overwhelming majority — sub-second to ~20–30 s (fleet p95 < 14 s) | inline result on the dispatch request | zero |
| Async | the tail, up to the 300 s budget ceiling | in-process task + completion callback | zero workflows; one in-memory registry entry for the task's lifetime |

Tier choice is made **per app, statically at registration** from the app's
declared budget (an app whose `preflight_gate_timeout_seconds` exceeds the
sync window registers async-backed operations), with per-request dynamic
escalation (return inline if the check happens to finish inside the sync
window, else escalate to a token) as a Phase 0 spike item — an optimization,
not a dependency.

The sync window itself: raise `component.nexusoperations.request.timeout`
from 10 s to ~20–30 s. Two facts bound how far to take it — the effective
handler window is *less* than the configured value (the timeout is measured
from the calling History Service and transits matching before the worker sees
the task), and a sync overrun is retried from zero (a sync call has no
checkpoint). Both push long work to the async tier rather than to a cranked
sync window: a 300 s sync setting would pin a request across History Service →
matching → worker for its full duration — the held-connection anti-pattern
this ADR retires — and a blip at second 250 would re-run the entire probe
against the customer's source.

### The durability trade — stated plainly

Rejecting workflows on this plane buys zero per-call orchestration and zero
workflow-management overhead, and costs **mid-flight durability**: if a worker
restarts while a slow check is running, the in-process task dies, no
completion is ever delivered, and the caller times out at the operation's
schedule-to-close and may retry.

This trade is correct *for this plane* because handler checks are idempotent,
read-only probes whose retry is cheap and whose loss is harmless: an
interactive user clicks again; Continuous Pre-flight's next tick re-probes;
nothing downstream depends on a lost probe. The one path where a lost check
would matter — the pre-extraction gate deciding whether a run proceeds —
**already rides inside a durable workflow** and keeps doing so. Durability is
applied exactly where it earns its cost, and nowhere else.

### How each use case is served

| # | Use case | Path | Workflow created? | History footprint |
| --- | --- | --- | --- | --- |
| 1 | Interactive, cloud app | Heracles → Nexus HTTP → app worker → handler | **No** | none |
| 2 | Pre-extraction gate | unchanged: `{app}:preflight` activity inside the run | **No new one** — inside the existing run | in the run's own history, as today |
| 3 | Interactive, SDR app | Heracles → Nexus HTTP → endpoint routes to the agent's task queue → SDR worker → handler | **No** (sync) | none |
| 3b | Interactive, slow source (up to 300 s) | same call; async token + in-process check + completion callback to the caller-supplied URL | **No** | none |
| 4 | Continuous Pre-flight | workflow-health's own sweep (its one pre-existing cron workflow) calls Nexus operations with `trigger=continuous`, never enforcing; async completions wake the sweep natively | **No per-probe workflow** | none — nothing in customer run history, nothing in visibility |

**"Is it the same across all implementations?"** Same handler, same contract,
same resolution, same classification, same telemetry — one invocation core.
Two invocation *forms* remain, honestly distinguished by where the caller
stands: outside a run → Nexus operation (sync or async by app budget); inside
the run → the gate activity. Both are thin skins over the same core.

**"Is it invoked via an API? Does it run a workflow?"** Yes — a plain HTTP
call against the Temporal frontend (or a native Nexus call from a workflow the
caller already owns). It runs **no workflow in any tier**: fast checks return
inline; slow checks are an in-process task plus a completion callback; the
gate rides a workflow that exists regardless.

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
keeps the workflow-per-call envelope (and its ~93 % overhead plus the
workflow-management overhead) for every SDR invocation; keeps two transports
forked inside the server; the dispatch layer (queue derivation from
agent-json, poller pre-check, pickup/job budget split) is bespoke
infrastructure that Nexus provides natively.

### Option 3: Workflow-per-call everywhere (extend `sdr:*`)

Rejected. Maximizes the measured orchestration overhead, pollutes visibility
with probe executions, still needs the credential/dispatch/classification gaps
closed, and still requires a Temporal client in every caller.

### Option 4: Actor pattern — long-lived per-agent workflow + synchronous Update

One always-running workflow per agent; callers send Updates as RPC.
Workable fallback if Nexus were unavailable: no per-probe execution, warm-path
latency comparable. Rejected: re-implements RPC on workflow machinery,
accumulates history requiring continue-as-new churn, needs per-agent singleton
lifecycle management, callers still need Update plumbing — and it is precisely
the *managed-workflow overhead* this ADR exists to remove.

### Option 5: New channel (NATS request-reply / reverse tunnel / WebSocket broker)

Rejected on the channel-selection rule: the customer already operates exactly
one authenticated, firewall-traversing, monitored channel — the worker's
outbound gRPC poll. A second channel duplicates security review, credential
management, HA, and monitoring on both ends to buy a capability Nexus provides
on the existing one. Never add a second hole; change the protocol on top of
the existing hole.

### Option 6: Nexus with workflow-backed async operations (this ADR's first draft)

Sync operations for the fast path; the slow tail escalates to async
operations backed by the existing `sdr:*` workflows.

Rejected after review: it preserves workflow-per-slow-call and — the deeper
cost — keeps the `sdr:*` workflows alive as managed artifacts (replay
determinism, versioning, registration, timeout churn, visibility noise)
forever, for a tail that needs none of workflow durability. The revised
decision replaces the backing with protocol-native manual completion.

### Option 7 (chosen): Nexus with manual-completion async operations

RPC semantics over the machinery already deployed: service/operation
registration on the worker (FastAPI-shaped developer experience, Temporal
substrate), frontend-brokered routing, sync ops with zero execution footprint,
and a slow tier that is an in-process task plus an outbound completion
callback — **no workflow in any tier**, no app server, no new channel, no
customer-side change.

Per-call cost comparison:

| Option | New infra | Per-call cost | Workflows to manage | Customer exposure |
| --- | --- | --- | --- | --- |
| Workflow-per-call (today / Option 3) | none | ~23 events, 4+ queue round trips, an execution record | 3 per app, forever | none |
| FastAPI facade (Option 2) | always-on server pod per app | in-process (cloud) / workflow (SDR) | 3 per app, forever | none |
| Nexus, workflow-backed async (Option 6) | endpoint records | ~1 round trip (fast) / workflow (slow tail) | 3 per app, forever | none |
| **Nexus, manual completion (Option 7)** | endpoint records | ~1 round trip (fast) / task + 1 callback POST (slow) | **zero** | none — same poll + outbound HTTPS |

## What Changes Where

| Layer | Change | App-author impact |
| --- | --- | --- |
| SDK | `create_worker` registers the `handler-ops` Nexus service bound to the existing handler; the shared invocation core is extracted (resolution, dispatch, budget, classification, events); additive `credential_guid` + `extraction_method` fields on `AuthInput` / `PreflightInput` / `MetadataInput`; async-tier operation registry + completion delivery; `sdr:*` workflows deleted at end of migration | **zero — inherited via version bump** |
| Platform / Temporal | enable Nexus on per-tenant deployments (dynamic config + HTTP dispatch surface); raise the sync window to ~20–30 s; endpoint provisioning per app (cloud) and per agent (SDR), automated in tenant tooling; authorizer claim mapping for Nexus HTTP; callback-endpoint reachability for agents (outbound HTTPS) | zero |
| Heracles | base URL swap: app-server service → Temporal frontend Nexus URL; a small in-VPC callback endpoint for async completions; response mapping; retire per-app HTTP service discovery | zero |
| Helm / atlan monorepo | remove handler deployment + service from app subcharts; `minReplicas` knob on worker for interactive apps; KEDA scaler updated for Nexus task backlog | zero |
| workflow-health | consume Nexus with `trigger=continuous` from its one pre-existing sweep workflow | zero |
| Apps | none mandatory; optional per-app declarations (gate mode, budget) unchanged — the declared budget now also selects the app's Nexus tier | **zero code** |

Fleet delivery: SDK release → renovate bump → conformance rule flags stragglers
→ remediation loop closes them. No per-app PRs.

### Migration sequence

0. **Spike (gates this ADR).** Prove on a staging tenant: (a) a custom async
   operation handler in the Python SDK returning an operation token from
   `start()` — the underlying `nexus-rpc` package (pinned `1.4.0` in
   `uv.lock`) defines the `OperationHandler` contract; Temporal's Python docs
   currently document only `@sync_operation` and `@workflow_run_operation`,
   so first-class support must be demonstrated, not assumed; (b) manual
   completion delivery from a NAT'd worker to the caller's callback URL,
   including the Temporal-caller form (callback into the caller cluster's
   Nexus machinery) and the HTTP-caller form (caller-supplied URL); (c) the
   sync window raised via `component.nexusoperations.request.timeout`;
   (d) per-request dynamic sync/async escalation (optimization — its absence
   does not block).
1. Extract the shared invocation core; rewire the gate activity and the HTTP
   routes onto it with behavior-preserving policies (pure refactor, lands
   independently and pays for itself even if Nexus stalls).
2. Register the Nexus service on workers behind a flag; provision endpoints on
   a staging tenant; prove all four use-case paths end-to-end, including a
   forced worker restart mid-check (verify clean caller timeout + retry).
3. Cut Heracles over per tenant; #2914's dispatch layer is bypassed.
4. Continuous Pre-flight onboards as a Nexus consumer.
5. Remove handler deployments from subcharts; **delete the `sdr:*` workflows**;
   mark ADR-0009 superseded on the handler side.

## Preconditions and Risks

- **Python SDK support for manual-completion async operations (the gating
  risk).** The nexus-rpc protocol supports it (caller-specified callback URLs,
  completion-delivery requests, a `CompletionHandler` on the caller side);
  Temporal's Python feature guide documents only the sync and
  workflow-run forms. If the spike finds the Python surface not yet usable,
  the fallback *within this ADR's rules* is: sync tier for everything within
  the raised window, plus per-app `checks_to_run` decomposition so no single
  call exceeds it — and the ADR's async tier waits for SDK support rather
  than falling back to workflow backing.
- **Callback reachability.** The completion callback is outbound HTTPS from
  the worker. For Temporal-workflow callers it targets the caller cluster's
  Nexus callback endpoint — verify agents' egress allows HTTPS to the same
  host they already reach over gRPC. For HTTP callers it targets a
  caller-supplied in-VPC URL (Heracles). Delivery retries and their ceiling
  must be confirmed in the spike.
- **Schedule-to-close for async operations.** Size to the 300 s budget ceiling
  plus headroom (~330 s) so a lost completion surfaces as a bounded, typed
  timeout — the "worker died mid-check" signal.
- **Sync-window ceiling.** Raised, not cranked: ~20–30 s. The effective
  handler window is less than the configured value, and sync overruns retry
  from zero. Long work belongs to the async tier (see "Tier selection").
- **Authorization.** Nexus HTTP dispatch must sit behind the Temporal
  authorizer with namespace-scoped claims, in-VPC only; the caller-supplied
  callback URL must be validated against an allowlist (an attacker-controlled
  callback URL would otherwise turn completion delivery into SSRF from
  customer infra). Inputs carry credential *references*, never values.
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
- **Worker-restart deploy hygiene.** Rolling worker deploys kill in-flight
  slow checks (bounded caller timeout, then retry). Acceptable for probes;
  worth a drain-grace note in the deploy runbook so interactive users see it
  rarely.
- **Transitional coexistence.** HTTP endpoints remain during migration; the
  facade and Nexus answer identically because both sit on the shared core.
  The compat window and its end date are part of Phase 3's definition of done.

## Open Questions

1. Exact Nexus request-timeout cap and callback-retry policy on our server
   build, and whether either is settable per endpoint rather than
   cluster-wide.
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
6. Per-request dynamic sync/async escalation (finish-inline-if-fast) versus
   static per-app tier selection from the declared budget — spike item 0(d).
7. Whether Heracles prefers the callback endpoint or a poll-with-wait
   retrieval for async interactive results (both are protocol-native;
   callback is the default proposal).

## Relationship to Prior ADRs

- **ADR-0001 (per-app handlers): unaffected.** The handler contract is the
  fixed point of this design.
- **ADR-0009 (separate handler/worker deployments): superseded for the handler
  side** once Phase 5 completes. Its cost rationale (scale-to-zero workers)
  survives; its always-on-server rationale is met by Nexus + per-app
  `minReplicas` instead of a second deployment.
- **ADR-0016 (multi-pool worker routing): compose.** Nexus endpoint → task
  queue routing must target the correct pool queue for apps using pools (see
  Open Questions).
