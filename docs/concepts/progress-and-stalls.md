# Progress and Stalls

A healthy-but-quiet task looks exactly like a wedged one. Both emit nothing, so any
watchdog that kills on silence has to guess which it is holding — and the SDK's answer
is to stop guessing: make every quiet spot *observable*, then fail only the attempts
that go quiet with nothing to vouch for them.

That is the **stall watchdog**. It runs inside your activity, alongside the
auto-heartbeat loop, and it asks one question on every tick: *how long has it been
since anything observable happened?* When the answer exceeds this task's
`max_no_progress_seconds`, the gap is reported — and, in an app that enforces, the
attempt is failed with a `TaskStalledError` naming the last signal it saw.

This page is what an app author needs in order to act. [ADR-0018](../adr/0018-progress-aware-heartbeat.md)
is the design record behind it; you should not need to read it to fix your app.

!!! note "Rollout status"

    The progress signals described here — the framework hooks, the automatic holds and
    `holding_progress()` — are live now. The two settings that *act* on them,
    `progress_watchdog` and `max_no_progress_seconds`, arrive together with the 24h
    `start_to_close` backstop in the release that turns the watchdog on fleet-wide in
    `warn`. Until then `timeout_seconds` keeps its 600s default and nothing observes the
    signals. Nothing below changes when that release lands; it is written for the state
    you will be in.

---

## The one rule

> **If any single step can run longer than `max_no_progress_seconds` without emitting
> a progress signal, that step must be made observable.**

Observable means one of exactly three things, and the first one is free:

| # | Mechanism | Who does it | Covers |
|---|---|---|---|
| 1 | **Framework hooks** | the SDK, automatically | streaming writes, object-store transfers, `FileReference` sync, the SQL/REST template page loops |
| 2 | **Automatic holds** | the SDK, automatically | anything offloaded through `run_in_thread` / `run_fault_isolated` |
| 3 | **A manual beat or a declared hold** | you, one line | a custom async loop, and any opaque single `await` against your own source client |

Mechanism 3 is not a rare escape hatch. See [holding_progress()](#holding_progress-the-part-you-write)
below — nearly every connector needs it at least once.

## What counts as progress

Progress is defined operationally, not by intuition:

> **Progress = one observable unit of work completing** — a batch written, a chunk or
> file transferred, a page fetched, or an explicit `self.heartbeat(...)`.

It is emphatically **not** wall-clock time, and **not** event-loop liveness. Loop
liveness is what the unconditional keepalive already proves, which is precisely why
the keepalive cannot double as the stall detector: a task that beats every 10s while
its source connection hangs is exactly the failure this watchdog exists to catch.

By construction, **any gap between progress signals longer than the budget is a
stall.** That is the intended behaviour, not a corner case — it is how a
wedged-but-quiet activity gets caught. The design's job is to make sure the
*legitimate* quiet spots are the small, known set that hooks and holds already cover.

## The three knobs, and which one is which

These are three different questions with three different answers. The third is new,
and it is the one most likely to be misread.

| Knob | Default | The question it answers | Do you tune it? |
|---|---|---|---|
| `heartbeat_timeout_seconds` | 60s | *"Is anything beating at all?"* Detects a dead process — OOM kill, SIGKILL, node loss, partition, a fully starved event loop. **Unchanged by ADR-0018.** | No |
| `max_no_progress_seconds` | 900s | *"How long may this attempt be silent before I call it stalled?"* Detects wedged-but-alive: the loop is fine, the beats are going out, nothing is advancing. | Rarely — it is roughly app-independent |
| `timeout_seconds` (`start_to_close`) | a 24h backstop | *"What is the absolute ceiling on one attempt?"* A last-resort bound, not a duration budget. | **No.** This is the number you used to guess; stop guessing it |

Three clarifications, because each is a real misread:

- **`max_no_progress_seconds` is not the beat interval.** The beat interval is
  `auto_heartbeat_seconds` (10s), it is unconditional, and it has nothing to do with
  the watchdog. Lowering the budget does not make your task beat more often.
- **`max_no_progress_seconds` is not a duration budget.** A task that runs for nine
  hours while writing a batch every thirty seconds never comes close to a 900s budget.
  The budget measures *gaps*, not totals. If you find yourself raising it because "my
  task takes a long time", the knob you actually want is a hold at the one call site
  that goes quiet.
- **`heartbeat_timeout_seconds` did not change meaning.** It still means "nothing is
  beating", it is still 60s, and it is still the only detector for a fully blocked
  event loop — which the in-process watchdog structurally cannot see, because a
  starved loop never runs the watchdog either. The two cover each other's blind spot.

There is a fourth setting, `progress_watchdog`, which is a mode rather than a
duration: `off`, `warn` or `enforce`. See [Modes](#modes-off-warn-enforce).

## What the SDK covers for you

### Framework hooks

These fire automatically. If your task's long steps run through them, you have nothing
to do:

| Where | Unit of work | Label |
|---|---|---|
| Streaming writers (`Writer._flush_buffer`, statistics sidecar) | one buffer chunk / the stats file | `writer.flush_buffer`, `writer.statistics` |
| Parquet writer | one accumulated / consolidated chunk | `writer.accumulate_chunk`, `writer.consolidate_chunk` |
| `RollingFileWriter` | one rolled chunk | `writer.rolling_flush` |
| Object-store byte loops | one multipart part / streamed chunk / range GET | `storage.upload_part`, `storage.download_chunk`, `storage.download_range` |
| Object-store transfers | one file transferred *or* skipped on a hash match | `storage.upload_file`, `storage.copy_file`, `storage.download_file` |
| External `CloudStore` upload | one part | `cloudstore.upload_part` |
| `FileReference` sync | one reference persisted / materialised | `file_ref.persist`, `file_ref.materialize` |
| SQL / REST template page loops | one fetched page written | `extract.page`, `fetch_databases.page`, `fetch_schemas.page`, `fetch_tables.page`, `fetch_columns.page` |
| Your own `self.heartbeat(...)` | whatever you decide | `task.heartbeat` |

Hooks sit on **batch, chunk and page boundaries — not per record, in the current hook
set.** One batch is already the unit of observable work, and a per-record mark would be
a hot-path cost for no extra signal.

The label matters beyond bookkeeping: it is what a stall report names, so
`last signal was 'fetch_tables.page'` tells an operator *which* loop went quiet rather
than only that something did.

### Automatic holds on offloaded work

A **hold** vouches for one in-flight operation the SDK cannot see into. While a hold is
in force the watchdog is paused, and when the hold is released the completed operation
counts as progress.

Every blocking call offloaded through `run_in_thread` is automatically wrapped in an
**unbounded** hold, so a legitimately long blocking call is never false-killed and
nothing at the call site changes. `run_fault_isolated` (and `run_best_effort` on top of
it) is held too, **bounded by that call's own `timeout`** — a number it already enforces
as a wall-clock kill of the child.

"Unbounded" is a real, deliberate residual: the watchdog is inactive for the whole call
and the 24h backstop is its only bound. That is the price of the SDK never inventing a
duration for somebody else's blocking call — and it is the second thing your warn report
tells you about, precisely because an auto-held call is invisible to any code audit.

See [Offloading blocking work](apps.md#offloading-blocking-work-run_in_thread-and-auto-holds)
for the call-site details.

## `holding_progress()`: the part you write

```python
# async: your own client, which the SDK cannot see into
async with self.holding_progress("snapshot metadata query", timeout=1800):
    rows = await self._client.execute(BIG_METADATA_QUERY)

# blocking: the same wrapper around the offload, to bound it
async with self.holding_progress("full table scan", timeout=7200):
    rows = await self.run_in_thread(cursor.execute, sql)
```

Reach it as `self.holding_progress(...)` or `self.task_context.holding_progress(...)`
inside an `App`, or import `holding_progress` from
`application_sdk.execution.progress` for app code outside the app class.

### Expect to need it

There is a genuine asymmetry between blocking and async work, and it is why this is
routine rather than exceptional:

- **Blocking source calls are already held.** `run_in_thread` is a mandatory seam —
  every blocking call goes through it — so the SDK can hold on your behalf.
- **Async source calls have no such seam.** Your connector talks to its source with its
  *own* async client (an async SQLAlchemy engine, an `httpx.AsyncClient`, a vendor SDK).
  The SDK's internal SQL and HTTP clients are for the SDK's own purposes and do not sit
  on your connector→source path, so there is nothing for the SDK to instrument.

Two things narrow the residual, but neither removes it:

- **Interleaved streaming reads need no hold.** The common extraction shape — fetch a
  page, write a batch, repeat — already marks progress on every write, so the read loop
  is covered as long as one fetch+write cycle stays under the budget.
- **A custom loop can beat instead.** `self.heartbeat(...)` marks progress, so a loop
  that beats once per iteration is observable without a hold.

What is left is the genuinely opaque **single** call: one large metadata query, one slow
list or export that returns everything at once. Almost every connector makes at least
one. For those calls a hold is **required, not advisory** — a forgotten one does not
fail in testing, it false-kills at the tail, against the largest tenant, hours in.

### What a hold actually does

- Inside the block, the watchdog is paused for this attempt.
- On exit, the completed operation is recorded as progress under `label`.
- Past the declared allowance the hold **lapses**: the watchdog resumes *from the
  deadline*, and the stall fires `max_no_progress_seconds` later. So a wedged held call
  is caught at **`timeout` + budget** rather than at the 24h backstop.
- `timeout=None` declares an **unbounded** hold — the backstop is the only bound. That
  is a legitimate choice, and warn mode reports it rather than hiding it.
- The allowance you declare **governs everything inside the block**, including the
  automatic holds `run_in_thread` would otherwise add. They stand down, so the blocking
  example above lapses at 7200s instead of inheriting an unbounded auto-hold that would
  outlive your allowance.
- Holds are keyed by token, so nested and concurrent blocks — an `asyncio.gather` over
  several opaque calls — never release each other. The hold is released in a `finally`,
  so an exception or a cancellation inside the block does not leave the watchdog paused.

!!! warning "High-cardinality labels"

    `label` is written into logs and metric labels, so it must identify a **site**.
    Never interpolate a query, a key, a credential or a customer value into it — a
    per-tenant label is a metrics-backend bill, not a code bug.

## Choosing an allowance

The allowance is **not a prediction of how long the operation takes.** It is:

> *How long would I let this one operation run before I would rather it failed?*

That question is answerable at the call site in a way "how long should this whole
activity take?" never was, because it is a property of one operation against one
resource rather than of a tenant's data volume.

Two things make it safe to answer:

**Take the number from your own data.** Every closed hold is measured, so the p99 for
that exact site is a query away — plus headroom. A number invented at the desk is the
thing this design exists to remove.

```promql
histogram_quantile(
  0.99,
  sum by (le) (
    rate(task_hold_duration_seconds_bucket{app_name="my-app", hold_label="full table scan"}[7d])
  )
)
```

**Err generous, because the error is asymmetric.** Too generous only delays detection
toward the backstop — mildly worse observability. Too tight kills a healthy run, and
because stall kills retry, a too-tight allowance burns the same wasted work up to three
times before the run finally fails. When unsure, round up.

## Reading your warn report

Warn mode observes and reports; it can never fail an activity. It emits exactly two
shapes, and they map to the two things worth acting on.

### Shape 1 — a no-progress gap with nothing vouching for it

The sites that need a hook, a beat or a hold.

```
Task 'fetch_tables' made no observable progress for 1043s (budget 900s);
last signal was 'fetch_tables.page' — not failing (warn mode)
```

```promql
# Which tasks and which quiet spots, ranked
topk(20,
  sum by (task_name, progress_last_label) (
    rate(task_no_progress_gap_seconds_count{app_name="my-app", watchdog_mode="warn"}[7d])
  )
)
```

`progress_last_label` is the last thing that *did* report before the silence, so it
points at the step immediately after it. `<none>` means the attempt never reported a
signal at all — usually a task that does all its work in one opaque call.

### Shape 2 — a long hold, unbounded above all

The sites that want an explicit allowance instead of the 24h backstop.

```
Task 'extract_metadata' held the stall watchdog at 'run_in_thread.Cursor.execute'
for 2110s with no declared allowance (no-progress budget 900s); this site is on the
work-list — wrap it in holding_progress(timeout=...) so a wedge here is caught at
allowance + budget rather than left to the duration backstop
```

```promql
# Long unbounded holds — the work-list, longest first
topk(20,
  sum by (task_name, hold_label) (
    rate(task_hold_duration_seconds_sum{app_name="my-app", hold_bounded="false"}[7d])
  )
)
```

A second line appears when a hold **lapsed** — your declared allowance was outlived, so
the watchdog resumed while the operation was still running. That is notable at any
duration, because too tight an allowance is what turns a healthy slow call into a false
kill once anything enforces:

```promql
sum by (task_name, hold_label) (rate(task_hold_duration_seconds_count{hold_lapsed="true"}[7d]))
```

Both shapes are a metric plus an **INFO** log — never WARNING. Under a fleet-wide
default a warn-mode observation is an expected observation, not an actionable failure,
and emitting it at WARNING would manufacture the alert noise this design exists to
reduce.

!!! note "One of these two metrics is also an alert surface"

    A *single* gap is your work-list, and nobody is paged for it. But **sustained**
    silence on one task is a wedged-but-alive attempt holding a worker slot, and while
    your app is in `warn` nothing will kill it before the 24h backstop — so
    `task_no_progress_gap_seconds` also backs the `AtlanAppTaskStalled` alert, which
    pages at an hour of accumulated silence per hour. If an operator brings you one,
    they arrive with the task name and the last progress label already in hand; the
    triage below is what they need from you. See the
    [stalled-task runbook](../runbooks/stalled-task.md) for their side of it.

    `task_hold_duration_seconds` is **not** alerted on — a long hold is a site that
    wants an allowance, not an incident.

### Triaging the list

Most of the list is *ignore it*. Work down it with this:

| What you see | What to do |
|---|---|
| A gap on a step that is genuinely one opaque call | Wrap it in `holding_progress()` with the p99 for that site plus headroom |
| A gap on a custom loop of your own | Add `self.heartbeat(...)` once per iteration |
| A gap whose `last_label` is an SDK hook, on a step you know is one slow query | The query is the gap, not the hook — hold the query |
| A long **unbounded** hold, comfortably under the backstop | Declare an allowance if you want a real bound; otherwise leave it. This is an optimisation, not a bug |
| A long **bounded** hold that stayed inside its allowance | Nothing. Somebody already sized this site |
| A **lapsed** hold on a site you believe is healthy | Raise the allowance — this one *will* false-kill once you enforce |
| A gap that only ever appears on one tenant, at one step | Look at the step before you change a number. This is what a real wedge looks like |

**Read the report from a large tenant.** A small tenant's fast steps hide the very gaps
that only open at the tail — which is where the original failures came from.

### Metric reference

| Metric | Type | Labels |
|---|---|---|
| `task_no_progress_gap_seconds` | histogram | `task_name`, `progress_last_label`, `watchdog_mode` |
| `task_hold_duration_seconds` | histogram | `task_name`, `hold_label`, `hold_bounded`, `hold_lapsed` |

`app_name` is inlined onto every series, and the rest of the app identity (version,
release channel, k8s topology) is reachable through `target_info` — see
[Monitoring](monitoring.md#whats-in-the-metric-body).

## Modes: `off`, `warn`, `enforce`

| Mode | Behaviour |
|---|---|
| `off` | Inert. Nothing is observed, nothing is reported. A kill-switch, not a normal state |
| `warn` | The watchdog runs and reports; it can never fail an activity. **The default** |
| `enforce` | Reports the gap, then fails the attempt |

Warn is the default fleet-wide because it cannot fail anything, which means nobody has
to opt in and every app starts producing its own work-list on upgrade.

### When a stall does fail an attempt

In `enforce`, the watchdog cancels the attempt and the SDK turns that into a
`TaskStalledError` — a subtype of `AppTimeoutError`, code `TIMEOUT_TASK_STALLED`,
carrying `stalled_for_seconds` and `last_progress_label`. It is **retryable on
purpose**: the dominant cause is a transient source-side hang that self-heals on a fresh
attempt, and a genuine wedge re-stalls at a cost of a few multiples of the budget rather
than of the backstop.

Detection latency is the budget plus at most one heartbeat tick.

Effective kill times:

| Situation | Killed at |
|---|---|
| Quiet step, no hold | `max_no_progress_seconds` |
| Wedged call inside a declared hold | `timeout` + `max_no_progress_seconds` |
| Wedged call inside an unbounded hold (including any `run_in_thread`) | the 24h backstop |
| Nothing beating at all (OOM, node loss, starved loop) | `heartbeat_timeout_seconds` (60s), unchanged |

**Before flipping an app to `enforce`, verify against a large-tenant / tail profile —
not a smoke test.** A small run hides the tail gaps, and because stall kills retry, an
under-instrumented app burns the same wasted work up to three times before failing.

## What if I do nothing?

Nothing fails differently. See
[Upgrading: the stall watchdog](../upgrade-guide-v3.md#step-11b-the-stall-watchdog)
for the full answer, including which code shapes do eventually need action and which
never will.

## Related

- [ADR-0018 — Progress-Aware Stall Watchdog and Duration-Backstop Timeouts](../adr/0018-progress-aware-heartbeat.md)
- [ADR-0010 — Async-First Design and Blocking Code Pitfalls](../adr/0010-async-first-blocking-code.md)
- [Tasks — Timeouts and auto-heartbeating](tasks.md#timeouts-and-auto-heartbeating)
- [Apps — Offloading blocking work](apps.md#offloading-blocking-work-run_in_thread-and-auto-holds)
- [Monitoring](monitoring.md)
