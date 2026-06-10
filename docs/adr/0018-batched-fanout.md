# ADR-0018: Batched Fan-Out (`map_batched`)

## Status
**Accepted**

## Context

Every `@task` method on an App becomes a full Temporal activity: the workflow
schedules it onto the task queue, a worker polls it off, and the result travels
back through the server. Each activity also writes several events to the
workflow's history (schedule / start / complete, plus command events) and adds
load to the self-hosted Temporal persistence layer.

Connectors commonly dispatch **one activity per item** — 10k tables = 10k
activities. That multiplies, by the item count, three things at once:

1. **Schedule-to-start latency** — every item pays a queue round-trip.
2. **History event count** — at ~4 events per activity, a 10k-item fan-out
   pushes a child workflow toward Temporal's hard **50,000-event history cap**.
   This is not theoretical: a large-workspace child workflow has been
   terminated by the history-service for exceeding the cap mid-run.
3. **Persistence write load** — bursts of activity events concentrate write
   pressure on the Temporal persistence/visibility DB.

The SDK had no shared helper for batching fan-out, so every connector
hand-rolled its own chunk/gather loop — inconsistently, and usually
rediscovering the payload-safety and retry-granularity caveats in production.

## Decision

Add **`App.map_batched(task, items, *, batch_size, input_factory,
max_concurrency=5)`** — a workflow-context helper that chunks `items`, invokes
the task once per chunk (the chunk is wrapped into the task's `Input` by the
explicit `input_factory`), runs chunks concurrently bounded by
`max_concurrency`, and returns one `Output` per chunk in chunk order.

Arguments are validated at call time with typed errors (ADR-0013:
`MapBatchedInvalidArgumentError`, an `InvalidInputError` leaf). Chunks are
workflow payloads, so `batch_size` × per-item size must stay under Temporal's
2 MB limit (ADR-0008) — chunk fields must be bounded with `MaxItems`, and large
items should travel as `FileReference`s.

Batching divides the per-item activity count (and the history events it
generates) by `batch_size`, which is what keeps a large fan-out's history under
the 50k-event cap.

## Options Considered

### Option 1: `map_batched` Helper on `App` (Chosen)

One shared fan-out helper on `App`.

**Pros:**
- **One idiom**: every connector batches the same way, with payload-safety and
  retry-granularity documented once
- **Contract-faithful** (ADR-0006): the task still takes exactly one `Input`
  and returns exactly one `Output`; `input_factory` makes the chunk-to-`Input`
  mapping explicit rather than magically constructing contracts
- **Call-time typed validation**: invalid `batch_size` / `max_concurrency`
  fail with an actionable `MapBatchedInvalidArgumentError`

**Cons:**
- Batching trades per-item retry granularity for fewer activities — a chunk
  retries together
- One more public API surface to maintain

### Option 2: Do Nothing (Not Chosen)

Keep activity-per-item and let apps hand-roll batching.

**Cons:**
- Fan-outs keep pushing child-workflow history toward the 50k-event cap as
  workspaces grow
- Every app reinvents chunking, ordering, concurrency bounding, and error
  propagation — inconsistently
- Payload-safety and retry-granularity caveats get rediscovered per app,
  usually in production

## Rationale

1. **History-cap safety is a correctness constraint.** The 50k-event cap is a
   hard limit that has already terminated production workflows; dividing the
   activity count by `batch_size` is the direct lever that keeps fan-outs under
   it (and incidentally cuts schedule-to-start latency and persistence load).
2. **Contract-faithful batching** (ADR-0006): the task still takes exactly one
   `Input` and returns exactly one `Output`; `input_factory` makes the
   chunk-to-`Input` mapping explicit rather than magically constructing
   contracts.
3. **Payload-safe by construction** (ADR-0008): chunks are workflow payloads,
   so the helper's contract requires bounded chunk fields and points large
   items at `FileReference`.

## Consequences

**Positive:**
- Fan-outs divide their activity and history-event count by `batch_size`,
  keeping large workspaces under the 50k-event cap
- One documented batching idiom with ordering, bounded concurrency, and typed
  call-time validation

**Negative:**
- Batching trades per-item retry granularity for fewer activities — a chunk
  retries together, and one chunk exhausting its retries fails the whole
  `map_batched` call

## Implementation

- `application_sdk/app/base.py` — `App.map_batched()`,
  `_DEFAULT_MAP_BATCHED_CONCURRENCY = 5`
- `application_sdk/app/base_errors.py` — `MapBatchedInvalidArgumentError`
- `docs/concepts/tasks.md` — batching pattern usage guidance
- `tests/unit/app/test_map_batched.py`
