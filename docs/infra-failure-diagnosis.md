# Infra-failure diagnosis and attribution

How the SDK turns a silent activity heartbeat timeout - the signature of an abrupt worker kill (spot reclaim, OOMKill, node loss) - into a recorded cause, by asking an out-of-band infra-event service what happened to the pod.

This is the companion action to the *worker eviction and SIGTERM* design (`docs/worker-eviction-and-sigterm.md`, tracked separately), which handles the *graceful* case. Read that first: it explains why abrupt kills never report `WorkerEvicted` and instead surface as heartbeat timeouts.

## TL;DR

- A heartbeat timeout with no preceding `WorkerEvicted` means the worker died before it could say why. The cause is only knowable out-of-band.
- On that timeout, the workflow runs a short `diagnose_infra_failure` activity that queries a **net-new infra-event service** (`{node, pod, window}` → `SPOT_RECLAIM | OOM_KILLED | NODE_LOST | NONE`), maps the verdict onto the SDK failure taxonomy, records it (Temporal search attribute + structured log), and **re-raises the original failure unchanged**.
- **Phase 1 (this scaffold) is attribution only.** It labels the failure; it does not change retry or failure behaviour. Recording the cause does **not** stop spot/OOM from consuming `max_attempts` - only rerouting the retry to a different task queue does, and that (Phase 2) needs a second on-demand worker pool that does not exist yet.
- Everything is behind `ATLAN_ENABLE_INFRA_FAILURE_DIAGNOSIS` (default off). When off, activity execution is byte-for-byte identical to today.

## Why this can't be done in-process

Three facts are load-bearing, and together they force the shape:

1. **The dead worker cannot diagnose itself.** Spot/OOM kill the process before any handler runs (see the eviction doc). So whatever asks "why did it die" must run somewhere that survived - the workflow, on a live worker.
2. **Temporal retries never change task queue.** A `RetryPolicy` re-runs on the same queue. Anything that reacts to the cause must be workflow-orchestrated, not a retry-policy tweak. (This is why Phase 2 rerouting is a fresh `execute_activity`, exactly like the eviction loop.)
3. **Correlation needs an identity that outlives the kill.** To ask about a node, the workflow must know the node - but the killed activity reported nothing. Two carriers survive: the **last heartbeat details** (recorded server-side before death) and the **worker identity** on the `ActivityError`.

## Components

| Component | Where | Role |
|---|---|---|
| Identity seed | `infra_diagnosis.seed_activity_identity`, called from `activities.py` | Records `{node, pod, started_at}` as the first heartbeat detail so it survives into the timeout |
| Detection | `infra_diagnosis._is_heartbeat_timeout` | Recognises the abrupt-kill signature on the `ActivityError` |
| Correlation | `infra_diagnosis._decode_activity_location` | Extracts `{node, pod, window}` from heartbeat details, falling back to worker identity |
| Diagnosis activity | `infra_diagnosis.diagnose_infra_failure` | Short activity that queries the infra-event service; fails safe to `NONE` |
| Service client | `infra_event_client.HttpInfraEventClient` (+ `Fake`, `_Disabled`) | The SDK-side boundary to the net-new service |
| Classification | `infra_diagnosis.classify_infra_failure` | **Your policy**: verdict → SDK failure taxonomy |
| Recording | `infra_diagnosis._diagnose_and_record` | Upserts the `infra_failure_cause` search attribute + logs |
| Wrapper | `infra_diagnosis.execute_activity_with_infra_diagnosis` | Drop-in over the eviction loop at the single call site (`base.py`) |
| **Infra-event service** | **net-new, not in this repo** | Ingests spot/OOM/node events into a durable store; answers the lookup |

The infra-event service is the one net-new build. It exists because a *live* Kubernetes query at diagnosis time is unreliable - pods are garbage-collected and Events expire (~1h). The service ingests spot-interruption notices, pod `lastState.terminated.reason`, and node-lifecycle events (Karpenter, node-termination-handler) into a durable, queryable store.

## Interaction - happy path

```
 App @task on a spot pod          Temporal server            Workflow (live worker)        Infra-event service
        │                              │                            │                             │
        │ heartbeat {node,pod,ts} ────►│ (recorded server-side)     │                             │
        │ ...working...                │                            │                             │
        ☠ spot reclaim / OOM (SIGKILL, no time to report)           │                             │
        │                              │                            │                             │
        │        heartbeat_timeout elapses                          │                             │
        │                              │── ActivityError(cause=     │                             │
        │                              │     TimeoutError.HEARTBEAT)►│                             │
        │                              │      + last_heartbeat_details, .identity                  │
        │                              │                            │ _is_heartbeat_timeout ✓     │
        │                              │                            │ _decode_activity_location   │
        │                              │◄── execute diagnose_infra_failure(location) ──┐          │
        │                              │                            │                  └─ lookup ─►│
        │                              │                            │                  ┌── verdict │
        │                              │                            │◄─────────────────┘  OOM_KILLED
        │                              │                            │ classify_infra_failure      │
        │                              │◄── upsert_search_attributes({infra_failure_cause}) ─      │
        │                              │                            │ log(cause, pod, node)       │
        │                              │                            │ raise (original error)      │
```

## Interaction - fail-safe paths

Diagnosis is strictly additive. Every failure in the diagnosis path degrades to "record `NONE`, propagate the original error":

| Situation | Behaviour |
|---|---|
| Feature flag off | Wrapper is a pure passthrough to the eviction loop. No diagnosis activity, no extra history. |
| No node/pod decodable (OOM before first heartbeat, no worker identity) | `diagnose_infra_failure` returns `NONE` without calling the service. |
| Service down / times out / malformed body | Client returns `NONE`; activity returns `NONE`. |
| Service says `NONE` (no event found) | Recorded as `NONE` = "cause unknown" - do **not** infer "confirmed not-infra". |
| Diagnosis or recording raises | Caught in `_diagnose_and_record`, logged, swallowed. Original `ActivityError` always wins. |

Invariant: infra diagnosis can never fail, block, or alter a workflow beyond adding a search attribute and a log line.

## Data contracts

Heartbeat identity (seeded, first detail):
```json
{ "node": "ip-10-0-1-2", "pod": "myapp-abc123", "started_at": "2026-07-01T10:00:00Z" }
```

Infra-event service:
```
POST {base_url}/infra-events/lookup
  { "node": str, "pod": str, "since": iso8601, "until": iso8601 }
→ { "cause": "SPOT_RECLAIM|OOM_KILLED|NODE_LOST|NONE", "evidence": { ... } }
```

Recorded cause (`RecordedCause`), mirroring `application_sdk.errors`:
```
code       AppError.code          e.g. "RESOURCE_EXHAUSTED"
category   FailureCategory value  e.g. "RESOURCE_EXHAUSTED"
audience   Audience value         e.g. "PLATFORM"
retryable  bool
infra_reason  raw InfraCause      e.g. "OOM_KILLED"   ← the search-attribute value
```

## Identity carriers

Two ways the workflow learns where the activity ran, in priority order:

1. **Heartbeat details** - `seed_activity_identity` records the identity dict as the first heartbeat detail; the auto-heartbeat keepalive re-sends it, so `TimeoutError.last_heartbeat_details` carries it after death. Primary carrier; needs `NODE_NAME`/`POD_NAME` in the pod env.
   - **Limitation**: a task that later calls `heartbeat(progress)` manually overwrites these details. For those tasks, carrier 2 covers it.
2. **Worker identity** - `ActivityError.identity`, parsed from `"{pod}@{node}"`. Clobber-proof, but requires setting the Temporal worker/client identity to that form (not wired in this scaffold; see Phase 2 / hardening).

## Determinism and replay

- Workflow-context code (`_is_heartbeat_timeout`, `_decode_activity_location`, `classify_infra_failure`, `upsert_search_attributes`) is pure and replay-safe - no clock, no I/O, no randomness. `classify_infra_failure` **must stay pure**.
- All I/O is inside `diagnose_infra_failure` (an activity); its result is cached in history and replayed deterministically.
- The correlation window comes from the heartbeat payload's timestamps, never `workflow.now()`.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| Enable diagnosis + attribution | `ATLAN_ENABLE_INFRA_FAILURE_DIAGNOSIS` | `false` |
| Infra-event service URL | `ATLAN_INFRA_EVENT_SERVICE_URL` | `""` (disables HTTP client) |
| Diagnosis activity timeout | `ATLAN_INFRA_DIAGNOSIS_TIMEOUT_SECONDS` | `30` |

## Wiring checklist (to turn Phase 1 on)

1. **Build the infra-event service** and point `ATLAN_INFRA_EVENT_SERVICE_URL` at it. Implement `HttpInfraEventClient.lookup` (there's a `# TODO(service)` with the shape).
2. **Fill `classify_infra_failure`** - the one policy decision (see below).
3. **Register the search attribute** `infra_failure_cause` (Keyword) on the Temporal namespace. Without it, `upsert_search_attributes` fails (caught, but no attribution).
4. **Expose pod identity** in the pod spec via the downward API:
   ```yaml
   env:
     - name: NODE_NAME
       valueFrom: { fieldRef: { fieldPath: spec.nodeName } }
     - name: POD_NAME
       valueFrom: { fieldRef: { fieldPath: metadata.name } }
   ```
5. **Set** `ATLAN_ENABLE_INFRA_FAILURE_DIAGNOSIS=true`. The diagnosis activity auto-registers on the worker (`worker.py`) and the seed activates.

The activity registration (`worker.py`) and the call-site wrapper (`base.py`) are already wired; both are inert while the flag is off.

## The one decision that's yours

`classify_infra_failure(verdict) -> RecordedCause` maps each infra cause onto your failure taxonomy, and it encodes real judgment, not mechanics. Natural leaves from `errors/leaves.py`:

| Verdict | Suggested leaf | Category / Audience | The question |
|---|---|---|---|
| `OOM_KILLED` | `ResourceExhaustedError` | RESOURCE_EXHAUSTED / PLATFORM | Leak (app owns, don't just re-run) or undersized limit (platform)? |
| `SPOT_RECLAIM` | `DependencyUnavailableError` | DEPENDENCY_UNAVAILABLE / PLATFORM | Infra, not the app's fault |
| `NODE_LOST` | `DependencyUnavailableError` | DEPENDENCY_UNAVAILABLE / PLATFORM | Same family as spot, or its own? |
| `NONE` | `AppTimeoutError` | TIMEOUT / APP_OWNER | Honest "timed out, cause unknown" |

## Phase 2 preview - rerouting (not built)

Once a second, on-demand worker pool exists on its own task queue, the `raise` in the wrapper becomes a routing decision:

```python
cause = classify_infra_failure(verdict)
decision = route_policy(cause)          # pure: (retry?, task_queue)
if decision.reroute:
    kwargs["task_queue"] = decision.task_queue   # workflow.execute_activity supports this
    return await execute_activity_with_eviction_retry(*args, **kwargs)   # fresh budget
raise
```

Prerequisites: the second worker pool + queue, a reliable queue for the diagnosis activity itself (today it shares the single queue - tolerable only because it's short and retried), and `route_policy`. Per current scope, OOM stays record-only even in Phase 2.

## Code references

- `application_sdk/execution/_temporal/infra_diagnosis.py` - detection, correlation, classify (your TODO), diagnosis activity, wrapper, identity seed
- `application_sdk/execution/_temporal/infra_event_client.py` - service client boundary + fake
- `application_sdk/execution/_temporal/activities.py` - `seed_activity_identity` call site
- `application_sdk/execution/_temporal/worker.py` - diagnosis activity registration (flag-gated)
- `application_sdk/app/base.py` - the single call site routed through the wrapper
- `application_sdk/constants.py` - the three config vars
- `tests/unit/execution/test_infra_diagnosis.py` - full path coverage, temporalio `TimeoutError` contract pin

## Related

- *Worker eviction, SIGTERM, and activity retries* (`docs/worker-eviction-and-sigterm.md`, tracked separately) - the graceful counterpart; explains why abrupt kills become heartbeat timeouts.
