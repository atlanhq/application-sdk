# ADR-0018: One Pod Per App, and One Check Core Behind Every Ingress

## Status

**Accepted** — BLDX-1612.

> **Supersedes [ADR-0009](0009-separate-handler-worker-deployments.md)** (separate handler
> and worker deployments). Both premises ADR-0009 rested on have since lapsed; see
> §Why ADR-0009 No Longer Holds.

## Context

Two problems turned out to be the same problem.

**The check paths had diverged.** The same `Handler.preflight_check` was reached from four
call sites, each built independently:

| | Input assembly | Credential resolution | Budget | Extra checks | Outcome event |
|---|---|---|---|---|---|
| Config UI (HTTP) | v2 wire normalizers | inline body creds **only** | none | none | none |
| SDR connectivity test | wire input passed through | `agent_json` only | env vars | object-store probe | none |
| Pre-run gate | extraction snapshot | guid + agent + named refs | enforced net of resolution | none | **the only one** |
| Boot | n/a | n/a | env var | object store only | none |

The consequences were not hypothetical. The config UI could not dereference a
`credential_guid` at all, so "Test connection" checked whatever was in the form and never
the stored credential a run would actually use — the single largest source of "it passed in
the UI and failed on the run". The pre-run gate never ran the object-store probe, so the
one caller in a position to stop a doomed run never checked the artifact path that run
depended on. Only the gate enforced a budget, so a handler sizing its probes to
`input.timeout_seconds` on any other path was sizing to a number nothing honoured. And only
the gate recorded anything, so "is this app's UI green while its runs are red?" was
unanswerable.

**A fourth path was missing.** Nothing checked proactively. Access revoked on Tuesday was
discovered by Thursday's scheduled run failing.

**And the deployment shape had drifted from its rationale.** ADR-0009 put handlers and
workers in separate deployments because handlers were always-on (`minReplicas: 1`) for
instant interactive responses while workers scaled to zero. In practice both are now
KEDA-managed and both scale to zero.

## Decision

### 1. One check core, four thin ingresses

`application_sdk/checks/` owns everything that decides the answer: credential resolution
across all four reference shapes, the budget enforced net of resolution, the timeout that
classifies *at* the deadline, the source-unverifiable vs plumbing-broken classification, the
standard check augmentations, the outcome row, and the projections. It was built by lifting
the pre-run gate's implementation — by far the most complete — rather than by settling on
what the four paths already agreed about.

Each ingress keeps only what is genuinely its own:

* **Pre-run gate** — posture. Whether a `NOT_READY` verdict aborts the run
  (`App.preflight_gate_mode`) is a decision no other caller has.
* **Config UI / SDR** — the answer to "a source we could not verify". Both surface it as an
  error, because a human is waiting; the gate reports it and lets posture decide, because
  nobody is.
* **Scheduled** — never raises, and returns a cadence recommendation.

Verdict, enforcement, and projection stay separate concerns. The handler is never consulted
about any of them.

### 2. Authentication is a preflight check, not an operation

`test_auth` becomes an `AUTH`-depth preflight run. `Handler.test_auth` is no longer abstract:
it delegates to `preflight_check` and projects the verdict onto the auth contract, so a
connector implements credential checking once. Overriding still works and still wins — and
warns, naming **v4.0** as the removal. `CheckDepth` (`AUTH` → `REACHABILITY` →
`PERMISSIONS` → `FULL`) is the portable vocabulary that makes this possible, and also lets a
frequent scheduled probe ask for a cheap run without knowing anything connector-specific.

### 3. Proactive drift detection, advisory first

The `sdr:*` workflows were only ever "SDR" by deployment accident — generic, per-app,
durable wrappers are what an interactive test needs on *any* deployment. They are now also
registered as `checks:*`, plus `checks:scheduled_preflight` for the periodic probe.

Cadence is adaptive without any new datastore: the SDK returns `recheck_after_seconds`
derived from the verdict (a failed credential earns 15 minutes; an all-green full pass earns
24 hours), and the **Automation Engine** owns the timer and the history. That split puts the
interpretation of a verdict with the checks and the scheduling with the scheduler, and needs
no cross-team agreement beyond "honour this number".

A scheduled `NOT_READY` blocks nothing and pauses nothing. Enforcement stays with the
pre-run gate, whose soft/hard posture ladder already exists for exactly this.

### 4. One pod per app (`--mode combined`)

One deployment per app running worker + FastAPI in a single process, KEDA-scaled on Temporal
queue depth.

## Why ADR-0009 No Longer Holds

ADR-0009's case was cost and latency. Measured on this checkout (Python 3.11, `ru_maxrss`
peak, `du` on site-packages):

| | peak RSS | `sys.modules` | on disk |
|---|---|---|---|
| bare interpreter | 13 MB | 53 | — |
| `+ temporalio` | 55 MB | 547 | 50 MB |
| `+ fastapi[standard]` | 75 MB | 902 | +12 MB |
| SDK worker module only | 79 MB | 1016 | fastapi/starlette **not** imported |
| SDK handler service module | 88 MB | 1139 | imports fastapi + starlette |
| **both SDK modules** | **88 MB** | **1139** | — |

**A handler pod alone already costs what worker + handler cost together: 88 MB either way.**
Importing `handler.service` pulls the worker stack in, so a combined process is not the sum
of the two — it *is* the handler process with the worker registered in it.

So both halves of ADR-0009's premise have lapsed:

1. Handler pods scale to zero in practice, so the always-on / no-cold-start benefit is gone.
2. Co-locating the worker is free, so the second pod buys no memory isolation — it only
   doubles the pod count and the cold-start surfaces.

Fault isolation, ADR-0009's remaining argument, is largely retained in-process:
`run_combined_mode` supervises and restarts the worker independently of uvicorn, so a worker
crash does not take HTTP down.

### Why not drop FastAPI entirely (worker-only)?

It was the obvious way to reach one pod, and the numbers do not support it. Dropping FastAPI
saves **~9 MB RSS, ~123 modules, ~12 MB image** — against `temporalio` at 50 MB, `botocore`
at 25 MB, `obstore` at 12 MB, plus pandas/pyarrow/duckdb in any SQL connector. A ~1–2% image
win, for which we would give up:

* **MCP** — mounted on the FastAPI app (`handler/service.py`), exposing `@mcp_tool`
  activities. Re-adding an ASGI server to keep it hands the 9 MB straight back.
* **Dapr pub/sub** — Dapr *pushes* events to the app over HTTP (`/dapr/subscribe`,
  `/events/v1/event/{id}`). No listener, no event ingestion.
* **The Prometheus scrape endpoint** — worker-only falls back to Pushgateway; combined mode
  proxies everything through the in-process `/metrics`.
* **Static manifest reads** — `/workflows/v1/manifest`, `/input-contract`, `/configmap*`
  serve files baked into the image, and Heracles/AE call them synchronously. These are a good
  candidate for publish-time extraction, but that is separate work.
* **`fetch_metadata` payload headroom** — over Temporal, metadata trees fall under the
  payload cap; HTTP JSON has none.

Temporal becomes the canonical ingress for the check operations regardless — which is where
the real cold-start benefit lies, since a task queue durably buffers a request while the pod
starts, and an HTTP request to a zero-replica service has nothing holding it.

## Consequences

**Positive**

* One implementation of "can this app reach and use this source", so a UI result predicts a
  run.
* The config UI gains credential-reference resolution, an enforced budget, failure
  classification and an outcome row — none of which it had.
* The pre-run gate gains the object-store probe.
* Every path emits the queryable outcome row, so UI-green/run-red is answerable.
* Proactive drift detection exists, and costs one wake per connection per interval rather
  than a standing pod.
* One deployment per app, for ~0 extra memory over today's handler pod.

**Negative / to watch**

* **The outcome event now carries non-gate rows.** Every path emits
  `"Preflight gate outcome"` with a new `trigger` attribute. `gate_mode` is `"none"` on
  non-enforcing rows, so an existing `gate_mode='hard'` filter stays exactly as selective —
  but a query counting `outcome='would_block'` without filtering `trigger='pre_run'` will now
  include UI tests. **connector-pulse must add that filter.**
* Handler input is rebuilt from a normalized request rather than forwarded, so the object a
  handler receives is not the caller's instance. `timeout_seconds` now always carries the
  enforced budget.
* The pod-count win depends on a platform-side chart change collapsing the two deployments
  into one with a single KEDA `ScaledObject`. The SDK change is backwards-compatible either
  way — all three modes keep working.
* Combined mode exposes probes on `:8000` (FastAPI) *and* `:8081` (worker health). **`:8081`
  is the correct k8s probe target** — it carries the worker liveness window from BLDX-1552,
  which the FastAPI probes do not.

## Implementation

```
Deployment: my-app  (KEDA ScaledObject, 0 → N on Temporal queue depth)
└── application-sdk --mode combined --app app.connector:MyApp
    ├── Temporal worker — canonical ingress for checks
    │   ├── {app}:preflight              pre-run gate (enforces posture)
    │   ├── checks:preflight_check       interactive  (sdr:* alias retained)
    │   ├── checks:test_auth             interactive  (sdr:* alias retained)
    │   ├── checks:fetch_metadata        interactive  (sdr:* alias retained)
    │   └── checks:scheduled_preflight   drift detection, advisory
    ├── FastAPI :8000 — Dapr pub/sub, /metrics, manifest, MCP, /workflows/v1/*
    └── Health   :8081 — k8s probes (carries the worker liveness window)
```

All four check paths call `application_sdk.checks.runner.run_checks`.
