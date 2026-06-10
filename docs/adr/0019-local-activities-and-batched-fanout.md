# ADR-0019: Local Activities and Batched Fan-Out

## Status
**Accepted**

## Context

Every `@task` method on an App becomes a full Temporal activity: the workflow
schedules it onto the task queue, a worker polls it off, and the result travels
back through the server. Each invocation therefore pays a task-queue round-trip,
schedule-to-start latency, and one billable action. Across the fleet, action
counts and schedule-to-start latency are direct cost drivers.

Two usage patterns amplify this:

1. **Small, fast steps.** Tasks like path normalization or metadata shaping
   complete in milliseconds, yet pay the same per-activity overhead as an
   hour-long extraction.
2. **Activity-per-item fan-out.** Connectors commonly dispatch one activity per
   item (10k tables = 10k activities), multiplying both the action count and
   the aggregate schedule-to-start wait by the item count.

Temporal provides **local activities** (`workflow.execute_local_activity`) for
the first problem: they execute on the workflow worker without a queue
round-trip. Their constraints: no heartbeats, they should be short (seconds),
and they run on the workflow worker's pod. The SDK had no way to opt a task
into local execution, and no shared helper for batching fan-out.

## Decision

Two opt-in additions:

1. **`@task(local=True)`** — the task executes via
   `workflow.execute_local_activity` instead of `workflow.execute_activity`.
   Constraints are validated at class definition time (import-time, ADR-0008
   style): heartbeat options are rejected (local activities cannot heartbeat),
   and `timeout_seconds` is capped at 300 s — the framework's own short-task
   convention (the built-in `cleanup_files` / `cleanup_storage` tasks use
   300 s), half the default remote-task timeout. Retry options pass through
   (local activities retry on the workflow worker). No registration change is
   needed: the combined worker already registers every `@task` activity and
   Temporal uses the same definition for local execution. The per-activity
   eviction-retry loop is not applied to local tasks — a local activity dies
   with its workflow worker, and Temporal re-dispatches the whole workflow
   task to another worker.

2. **`App.map_batched(task, items, *, batch_size, input_factory,
   max_concurrency=5)`** — a workflow-context helper that chunks `items`,
   invokes the task once per chunk (the chunk is wrapped into the task's
   `Input` by the explicit `input_factory`), runs chunks concurrently bounded
   by `max_concurrency`, and returns one `Output` per chunk in chunk order.
   Arguments are validated at call time with typed errors (ADR-0013:
   `MapBatchedInvalidArgumentError`, an `InvalidInputError` leaf). Chunks are
   workflow payloads, so `batch_size` × per-item size must stay under
   Temporal's 2 MB limit (ADR-0008) — chunk fields must be bounded with
   `MaxItems`, and large items should travel as `FileReference`s.

## Options Considered

### Option 1: Opt-In `local=True` + `map_batched` Helper (Chosen)

Explicit, declarative opt-in on the existing `@task` decorator plus one shared
fan-out helper on `App`.

**Pros:**
- **Explicit**: the cost/durability trade-off is visible at the decorator and
  call site; no hidden behaviour changes for existing tasks
- **Fail fast**: invalid local-task configurations error at class definition,
  not at runtime in production (consistent with ADR-0008)
- **Zero registration changes**: the combined worker already registers
  workflows and activities together; local execution reuses the same activity
  definitions
- **One idiom**: every connector batches the same way, with payload-safety and
  retry-granularity documented once

**Cons:**
- Developers must decide per task; a wrong `local=True` on a slow task
  surfaces as a timeout (bounded at 300 s)
- Two more public API surfaces to maintain

### Option 2: Do Nothing (Not Chosen)

Keep every step a full activity and let apps hand-roll batching.

**Cons:**
- Fleet-wide action counts and schedule-to-start latency keep growing with
  connector adoption
- Small framework steps pay full activity overhead forever

### Option 3: Auto-Detect Small Tasks (Not Chosen)

Infer locality from observed task duration or static analysis and silently
switch fast tasks to local execution.

**Cons:**
- **Implicit magic**: execution placement changes without a visible code-level
  signal; debugging "why did this task lose its heartbeat?" becomes archaeology
- Duration is data-dependent; a task that is fast in dev may be slow in prod,
  flipping execution semantics silently (the same dev/prod inconsistency
  ADR-0008 rejects for payload limits)

### Option 4: Side-Channel Batching in App Code (Not Chosen)

Document the pattern and let every app implement its own chunking/gather loop.

**Cons:**
- Every app reinvents chunking, ordering, concurrency bounding, and error
  propagation — inconsistently
- Payload-safety and retry-granularity caveats get rediscovered per app,
  usually in production

## Rationale

1. **Cost is a first-class constraint.** Action counts and schedule-to-start
   latency are fleet cost drivers; both features attack them directly —
   `local=True` removes the round-trip for small steps, `map_batched` divides
   fan-out action counts by `batch_size`.
2. **Opt-in preserves durability defaults.** Remote activities with heartbeats
   remain the default; apps trade durability features for cost only where they
   choose to.
3. **Import-time validation** (ADR-0008 precedent): an invalid local-task
   configuration is a programming error and should fail the moment the class
   is defined, with a message that names the task and the fix.
4. **Contract-faithful batching** (ADR-0006): the task still takes exactly one
   `Input` and returns exactly one `Output`; `input_factory` makes the
   chunk-to-`Input` mapping explicit rather than magically constructing
   contracts.

## Consequences

**Positive:**
- Small steps stop paying queue round-trips and actions; fan-outs divide their
  action count by `batch_size`
- Misconfiguration fails at class definition with actionable messages
- One documented batching idiom with ordering, bounded concurrency, and typed
  call-time validation

**Negative:**
- Local tasks lose heartbeats and standalone-worker placement: they run on the
  workflow worker's pod and must finish within 300 s
- Batching trades per-item retry granularity for action count — a chunk
  retries together, and one chunk exhausting its retries fails the whole
  `map_batched` call
- Local tasks bypass the eviction-retry loop; recovery from worker eviction
  relies on workflow-task re-dispatch instead

## Implementation

- `application_sdk/app/task.py` — `local` parameter on `@task`,
  `TaskMetadata.local`, `LocalTaskConfigurationError`,
  `_MAX_LOCAL_TASK_TIMEOUT_SECONDS = 300`
- `application_sdk/app/base.py` — `_create_task_activity_wrapper(local=...)`
  routes through `workflow.execute_local_activity`; `App.map_batched()`
- `application_sdk/app/base_errors.py` — `MapBatchedInvalidArgumentError`
- `docs/concepts/tasks.md` — usage guidance (when to use local tasks, when
  not, batching pattern)
- `tests/unit/app/test_task.py`, `tests/unit/app/test_base.py`,
  `tests/unit/app/test_map_batched.py`
