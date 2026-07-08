# ADR-0017: Durable Page Iteration (`App.iterate`) — Bounding Workflow History Without Heartbeat-Timeout Trade-offs

## Status

**Accepted** — motivated by the Workflow-Resiliency triage (Temporal history-cap
force-terminations of large connector runs). Implements the two patterns Temporal recommends
for high-cardinality work; builds on the existing `App.continue_with` primitive (ADR-0010,
async-first) and the heartbeat machinery.

## Context

Temporal enforces a hard **51,200-event / 50 MB** history cap per workflow execution (warning at
10,240). Large connector runs — lineage, query-history miners, and similar on high-volume tenants
— fan out **one activity per item**, chunked with a manual `range()` + `asyncio.gather` inside a
single workflow run (see `application_sdk/templates/incremental_sql_metadata_extractor.py`). Each
activity contributes several history events, so history grows **linearly with the item count** and
the history-service force-terminates the run mid-flight. The failure is silent: lineage /
query-intelligence comes back quietly incomplete for exactly the largest tenants, and it counts
against the fleet success rate.

Two failure modes are in tension:

1. **History-cap termination** — too many activity events in one workflow.
2. **Heartbeat timeout** — an activity that runs too long without heartbeating.

Batching the *number* of activities (one activity per chunk) only divides history by a constant
factor (`O(n / batch_size)`); to keep the largest tenants under the cap you must grow the chunk,
which lengthens each activity and pushes it toward the **heartbeat-timeout** failure mode. That
coupling — one knob trading one production failure for another — is the thing to eliminate.

## Decision

Add **`App.iterate(page_task, *, cursor, input_factory, next_cursor, resume_input,
history_threshold=None, max_pages_per_generation=None)`** — a workflow-context driver whose
**primary mechanism is the root-cause fix** and which keeps a platform-mandated safety net for the
extreme tail.

**The fix (primary): one heartbeating page-activity, not one activity per item.** The author writes
a single `@task` that processes a *whole page* internally, calling `self.heartbeat(cursor)` per item
and offloading blocking work through `self.run_in_thread`. That page is **one** activity — a handful
of history events — regardless of page size, and page size is bounded only by memory / payload,
**never** by the heartbeat timeout (heartbeating *inside* the activity is the canonical
timeout-prevention mechanism, already automatic via `auto_heartbeat_seconds`). This directly removes
the "unbounded number of granular activities" root cause: at ~4–6 events per activity and a ~40K
usable budget, a single run absorbs **millions of items** in coarse pages with no history reset at
all. For essentially every real connector, this lever alone keeps history bounded.

**The safety net (last resort): continue-as-new between pages.** 50K events is a hard Temporal
limit, so a tenant large enough that even max-sane pages exceed the budget must reset history
*somehow* — there is no third option (continue-as-new or child workflows). After each page,
`iterate` checks `workflow.info().is_continue_as_new_suggested()` and, only when Temporal itself
reports history is approaching the cap, restarts the workflow with a fresh history via
`continue_with`, resuming at the current cursor. **In the common case this never fires** — the
page-activity coarsening has already kept history low. It is a backstop, not the feature.

This is deliberately *not* the misuse continue-as-new is warned against: granularity is fixed
**first**, so the safety net never papers over granular activities; each page's activity is
**awaited before** the boundary, so no activity is ever left pending across it; and the reset
happens only at clean page boundaries with the cursor carried in the input — the exact use
continue-as-new was designed for. The loop is deterministic, so it is workflow-replay-safe.
`history_threshold` is an advanced override for teams that want to force the reset earlier than
Temporal suggests; most callers leave it unset. Invalid `history_threshold` /
`max_pages_per_generation` raise `DurableIterateInvalidArgumentError` (ADR-0013 `InvalidInputError`
leaf) at call time.

## Options Considered

### Option 1: `App.iterate` — heartbeating page-activity + continue-as-new iterator (Chosen)

**Pros:**
- **Bounds history for any `n`** (reset each generation), not by a constant factor.
- **No heartbeat trade-off** — page size is decoupled from the history lever; heartbeating happens
  inside the page-activity.
- **Connectors stop hand-rolling** chunk loops, cursor checkpointing, and continue-as-new.
- Reuses existing primitives (`continue_with`, `heartbeat`/`get_last_heartbeat_details`,
  `run_in_thread`, `persistent_state`).

**Cons:**
- The page is the retry unit — a failed page retries together (mitigated by resuming from
  heartbeat details within the page-task).
- Requires the author to design a cursor that round-trips through `run()`'s `Input`.

### Option 2: `map_batched` — batch the number of activities (Not Chosen as the fix)

Chunk fan-out to one activity per chunk. **Cons:** history stays `O(n / batch_size)`; controlling
history means growing chunks, which reintroduces heartbeat risk. A weaker variant of Option 1's
lever #1 without the continue-as-new guarantee. May survive as a minor throughput knob but is
neither necessary nor sufficient for the history cap.

### Option 3: Child-workflow partitioning / sliding window (Deferred)

Temporal's answer for true parallel throughput (each child gets its own 50K budget; the official
`batch_sliding_window` sample). **Not viable today:** the SDK makes no
`execute_child_workflow`/`start_child_workflow` calls and inter-app wiring is deactivated under
BLDX-878. A separate, larger effort — revisit if a single continue-as-new chain can't keep up on
the biggest tenants.

## Consequences

**Positive:**
- Large fan-outs stay under the 51,200-event cap regardless of tenant size, without inducing
  heartbeat timeouts.
- One documented iteration idiom with cursor resume, ordering, and history bounding.

**Caveat — cleanup fires every generation:** `continue_with` runs `finally: on_complete()` on every
cycle, which schedules `cleanup_files` / `cleanup_storage` and deletes tracked `FileReference`s +
temp dirs. Cross-generation data must live in `persistent_state` / the upstream object store, never
in transient local temp expected to survive a boundary. `App.iterate`'s contract makes per-page
results the page-task's responsibility to persist, which respects this.

**Follow-up:** a conformance rule (H-series) flagging `asyncio.to_thread` / async-wrapped sync
activity bodies — the actual cause of most current production heartbeat timeouts — steering authors
to `run_in_thread`.
