# ADR-0018: Progress-Aware Heartbeat and Duration-Backstop Timeouts

## Status
**Proposed**

## Context

Every SDK `@task` becomes a Temporal activity carrying two timeouts (see
`application_sdk/app/task.py`, `application_sdk/execution/_temporal/activities.py`):

- **`start_to_close`** — the hard bound on a single activity attempt. Temporal
  *requires* either this or `schedule_to_close` on every activity; the SDK makes
  `timeout_seconds` non-nullable and defaults it to **600s** (`ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS`).
- **`heartbeat_timeout`** — the max gap allowed between heartbeats. Optional in
  Temporal; the SDK defaults it to **60s** (`ATLAN_HEARTBEAT_TIMEOUT_SECONDS`) and
  runs a background **auto-heartbeat loop** every 10s so app authors get liveness
  for free (`auto_heartbeat_loop` in `execution/heartbeat.py`).

The two timeouts catch *orthogonal* failure modes and neither subsumes the other:

- `heartbeat_timeout` detects **unresponsiveness** (crash, deadlock, network loss,
  event-loop starvation) — it only fires when heartbeats *stop*.
- `start_to_close` detects **taking too long while healthy** — an activity that
  keeps heartbeating but runs longer than allowed. It is the *only* bound on a
  healthy-but-slow or wedged-but-alive attempt.

### The problem: `start_to_close` is an unguessable number

`start_to_close` forces app authors to answer *"how long should this whole
activity take?"* — a number that scales with tenant size from seconds to days and
is data-dependent. In practice authors guess it, weight-class it, and still guess
wrong at the tail. A production failure dashboard (13-day window) showed a steady
trickle of `activity StartToClose timeout` failures across multiple connectors —
extraction, query-history, and processing activities — despite each app having
already tuned per-task timeouts (e.g. 5-minute "light", 2-hour "medium", 6-hour
"heavy" classes). The tuning is educated guessing, and the tail always exceeds it.

The intuitive fix — *"raise the `start_to_close` default to something enormous
like 24h and stop guessing"* — is directionally right (a generous
`start_to_close` backstop plus a fast watchdog is Temporal's own recommended
pattern for long-running work) but **unsafe as-is**, because of how the
auto-heartbeat currently works.

### Why a large `start_to_close` is unsafe today

The auto-heartbeat is an **unconditional keepalive**: `heartbeat_keepalive()`
calls `activity.heartbeat()` every 10s regardless of whether the activity is
making any real progress. It proves the *event loop is alive*, not that *work is
advancing*. Consequences:

1. `heartbeat_timeout` cannot catch a **wedged-but-alive** activity (an infinite
   async retry loop, a fetch that streams forever, a driver stuck in a
   yield-friendly wait). Heartbeats keep arriving, so only `start_to_close` ever
   stops it.
2. Therefore, if we raise `start_to_close` to 24h and keep the unconditional
   keepalive, a wedged activity is guarded by **nothing** for a full day →
   worker-slot exhaustion (Temporal's Python worker defaults to ~100 concurrent
   activity slots; a handful of stuck activities can starve the pool) and
   day-long non-detection.

The number that is genuinely hard to guess (total duration) cannot simply be made
huge unless *something else* reliably kills a stuck activity fast. That "something
else" must be the heartbeat — and today it can't do the job.

## Decision

Make the auto-heartbeat **progress-aware**, then invert the timeout model:

1. The auto-heartbeat loop sends a beat **only when a progress token advanced**
   since the last tick (or a sanctioned blocking op is in flight). A stall stops
   the beats, so `heartbeat_timeout` fires `heartbeat_timeout` seconds after the
   *last real progress*.
2. `heartbeat_timeout` is redefined as **"maximum acceptable time making zero
   forward progress"** — a roughly workload-independent number (minutes), unlike
   total duration.
3. `start_to_close` becomes a **pure backstop** (e.g. 24h) that authors never tune.
4. Opaque single blocking calls that emit no progress (one long query/API call)
   are kept alive by an explicit, scoped **liveness bracket**, with a
   resource-level timeout (DB `statement_timeout`, `httpx` timeout) as their real
   watchdog — consistent with ADR-0010's "blocking code must own its timeout."

This shifts tuning off the hard knob (duration) onto two answerable ones: *"how
long is no progress acceptable?"* (uniform default) and *"how long should one
query take?"* (a property of the resource).

### The progress token

`TemporalHeartbeatController` (`execution/heartbeat.py`) gains a monotonic
progress generation and a blocking-depth counter:

```python
class TemporalHeartbeatController:
    def __init__(self) -> None:
        self._last_details: tuple[Any, ...] = ()
        self._progress_gen: int = 0      # bumped on every real progress signal
        self._blocking_depth: int = 0    # >0 while inside a sanctioned blocking op

    def mark_progress(self) -> None:
        """Framework + app signal for real forward progress."""
        self._progress_gen += 1

    def heartbeat(self, *details: Any) -> None:
        # A manual heartbeat is, by definition, progress.
        self._last_details = details
        self._progress_gen += 1
        activity.heartbeat(*details)

    def enter_blocking(self) -> None: self._blocking_depth += 1
    def exit_blocking(self)  -> None: self._blocking_depth -= 1

    def heartbeat_keepalive(self) -> None:
        activity.heartbeat(*self._last_details)   # unchanged: the raw send
```

### Gating the loop

Only the *send* becomes conditional; the existing event-loop-block warning and
memory-pressure sampling still run every tick:

```python
last_seen_gen = -1
while not stop_event.is_set():
    ...  # existing wait_for(stop_event) + block-detection warning + memory sampling

    beat = (
        hb.progress_gen != last_seen_gen     # real progress since last tick
        or hb.blocking_depth > 0             # legit opaque op in flight
    )
    if beat:
        last_seen_gen = hb.progress_gen
        hb.heartbeat_keepalive()
    else:
        logger.debug(
            "No progress in last %.0fs for '%s'; withholding keepalive so "
            "heartbeat_timeout can fire if the stall persists",
            interval_seconds, task_name,
        )
```

That `else` branch is the entire behavioral change.

### Feeding the token (three sources, in priority order)

1. **Framework hooks (zero app effort).** Call `hb.mark_progress()` wherever the
   SDK already loops over units of work: the batched output writers, the
   record/statistics emission path, and the `ObjectStore` transfer loops
   (`storage/transfer.py`, `storage/chunked.py`, `storage/file_ref_sync.py`).
   This covers the streaming majority automatically.
2. **Manual `context.heartbeat(...)`** — already exists; now also bumps progress
   and still carries resume details for `get_last_heartbeat_details()`.
3. **The blocking bracket** — below.

### The opaque-call escape hatch

`run_in_thread` is already the sanctioned wrapper for blocking work and already
mandates an internal timeout (ADR-0010). Bracket it so a legitimate long single
call is not false-killed; hang-detection is delegated to the internal timeout,
with `start_to_close` as the ultimate backstop:

```python
async def run_in_thread(func, *args, **kwargs):
    hb = _current_heartbeat_controller()
    if hb: hb.enter_blocking()
    try:
        return await loop.run_in_executor(_BLOCKING_EXECUTOR, ...)
    finally:
        if hb: hb.exit_blocking()
```

For async-opaque single awaits with no progress signal, expose the same bracket
explicitly:

```python
async with self.context.holding_progress("snapshot metadata query"):
    rows = await long_single_query(...)   # its own statement_timeout is the watchdog
```

## Options Considered

### Option 1: Raise the `start_to_close` default to 24h and delete per-task tuning (Rejected as-is)

The originating proposal: stop guessing durations by making the default enormous
and removing the per-task constants.

**Why rejected on its own:** with today's unconditional keepalive, nothing kills a
wedged-but-alive activity for 24h → worker-slot exhaustion and day-long
non-detection (see Context). It also does not even fix the observed failures: the
apps in the dashboard *already* override the default on every task, so bumping the
default reaches none of them. This option is the *cosmetic half* of the real fix
and is only safe once Option 3 is in place.

### Option 2: Keep guessing, tune per-task durations harder (Rejected)

Continue the status quo — better per-task `timeout_seconds` guesses.

**Cons:** the number is fundamentally unguessable (scales with tenant/data); the
dashboard shows thoughtful guesses still failing at the tail. Unbounded tuning
that never converges.

### Option 3: Progress-aware heartbeat + duration backstop (Chosen)

Redefine the heartbeat as a progress watchdog so `heartbeat_timeout` catches
stalls fast; make `start_to_close` a backstop. Detailed above.

**Pros:**
- Eliminates the unguessable knob; the surviving knobs don't scale with tenant size.
- Kills wedged activities in minutes instead of a day → no slot exhaustion.
- Streaming work is covered automatically via framework hooks — zero app effort.
- Reuses the existing `heartbeat_timeout` knob and single auto-loop send site;
  small, localized change.

**Cons:**
- Requires framework hooks at the right loops, and an audit of opaque single-call
  tasks to bracket them (migration cost).
- Turning it on globally without the hooks would false-kill pure-async tasks that
  emit no progress → must be flag-gated during rollout.

### Option 4: Manual heartbeating only (Rejected)

Disable auto-heartbeat; require `context.heartbeat()` everywhere. Rejected for the
same reasons as ADR-0010 Option 3 — easy to forget, and it cannot heartbeat during
a single long opaque call. Progress hooks give the same signal without the discipline tax.

## Rationale

### The asymmetry that makes the inversion work

*"How long should the whole thing take?"* is workload-specific and unbounded.
*"How long is it acceptable to make zero forward progress?"* is roughly
workload-independent — a few minutes for almost any connector. One sane global
number can express the second; no global number can express the first. That
asymmetry is the entire reason a large `start_to_close` default becomes safe once
the heartbeat is progress-aware.

### Failure detection is preserved or improved for every mode

| Scenario | Before | After |
| --- | --- | --- |
| Event loop fully blocked (no `run_in_thread`) | auto-loop starved → killed at `heartbeat_timeout` | unchanged — still correct |
| Streaming work, healthy | beats unconditionally | beats on real throughput via hooks |
| Wedged-but-looping (the gap today) | only `start_to_close` (→ 24h under the naive proposal) | **killed at `heartbeat_timeout` after last progress** |
| Legit long opaque call | keepalive keeps it alive; bounded by `start_to_close` | blocking bracket keeps it alive; internal/resource timeout is the watchdog |
| `run_best_effort` child-process work (ADR-0017) | parent awaits; keepalive beats | parent awaits via `run_in_thread`/await → bracket keeps beating |

### What authors tune afterward

| Knob | Before | After |
| --- | --- | --- |
| `start_to_close` | the number everyone guesses wrong | 24h backstop, never tuned |
| `heartbeat_timeout` | "how often do I beat" (coupled to keepalive) | **"max no-progress window"** — ~5–10 min, roughly app-independent |
| opaque-call bound | none / `start_to_close` | resource-level timeout (DB `statement_timeout`, `httpx` timeout) |

## Rollout

Progress-gating cannot be turned on globally in one step: any existing task doing
pure-async work that never hits a framework hook or manual beat would start being
killed at `heartbeat_timeout`. Sequence:

1. **Ship the framework `mark_progress()` hooks first** (writers, emission,
   transfer loops) so the streaming majority is covered before anyone opts in.
2. **Gate behind a flag** — `progress_aware_heartbeat` on `@task` /
   `TaskMetadata`, defaulting **off**, plus an env kill-switch.
3. **Flip per-app after verification** — with hooks in place, the only migration
   work is auditing opaque single-call tasks and wrapping them in the bracket (or
   giving them a resource-level timeout).
4. **Then** raise the `start_to_close` default (e.g. → 24h) and retire the
   per-task duration constants — safely, because the watchdog is now real.

The AE-orchestrated DAG node timeouts (`errorHandling.startToCloseTimeoutSeconds`
in the contract toolkit, defaulting 24h/72h) are a *separate layer* consumed by
Automation Engine and are out of scope here; this ADR governs SDK-native `@task`
activities. The same "backstop + progress watchdog" philosophy could later be
applied there.

## Consequences

**Positive:**
- App authors stop guessing an unguessable duration; `start_to_close` becomes a
  set-and-forget backstop.
- Wedged-but-alive activities are detected in minutes, removing the slot-exhaustion
  risk that blocks a large `start_to_close` default.
- Streaming connectors get correct behavior with no code changes (framework hooks).
- Change is localized to `execution/heartbeat.py` (controller + loop +
  `run_in_thread`), the `activities.py` wiring, and one-line `mark_progress()` calls
  in existing SDK write/transfer loops.

**Negative:**
- Opaque single-call tasks must be audited and either bracketed or given a
  resource-level timeout — a one-time migration per app.
- A new default semantics for `heartbeat_timeout` must be documented so authors
  read it as "no-progress budget," not "beat interval."
- During rollout, the flag adds a temporary branch that must be removed once
  progress-aware heartbeat is the default (per the "delete v3-prep workarounds"
  discipline).

## Related

- **ADR-0010** — Async-First Design and Blocking Code Pitfalls (blocking code owns
  its timeout; `run_in_thread` keeps the loop responsive). This ADR builds directly
  on it: the blocking bracket reuses `run_in_thread`'s existing internal-timeout contract.
- **ADR-0017** — Native Execution Isolation (`run_best_effort` / `run_fault_isolated`).
