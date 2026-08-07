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

### The mental model: make quiet spots observable

The core realization: **a healthy-but-quiet task is indistinguishable from a
wedged one.** Both emit nothing. A watchdog that kills on silence cannot tell them
apart — so the only durable fix is to make every quiet spot *observable*, i.e.
emit a signal of forward progress that the watchdog can see.

The SDK already ships one observability primitive for this: the manual beat
(`context.heartbeat()`). This ADR does **not** invent a new concept — it extends
that same observability *holistically and, wherever possible, automatically*:

- **Automatic (no developer action):** built-in progress signals on the paths the
  SDK already owns — the streaming writer/emission/transfer loops, and anything
  run through `run_in_thread`.
- **Explicit (developer chooses one mechanism):** for the spots the SDK cannot see
  — a custom async loop, or an opaque single `await` against the connector's own
  source client — the developer makes it observable with a manual beat or a scoped
  bracket. This is not a rare edge: because the async source path has no mandatory
  SDK seam (unlike blocking calls, which all go through `run_in_thread`), the
  explicit bracket is expected in nearly every connector (see *The async escape
  hatch* below).

This framing matters for expectations: **the residual work never disappears.** The
ADR shrinks it to a small, auditable set and automates the common paths, but a task
that does long work through code the SDK can't see will still need one of the three
mechanisms. "Zero effort" is true only for the covered paths; everything else is a
deliberate, one-line observability choice.

### Inverting the timeout model

With observability in place, invert the two timeouts:

1. The auto-heartbeat loop sends a beat **only when a progress token advanced**
   since the last tick (or a bounded blocking op is in flight). A stall stops
   the beats, so `heartbeat_timeout` fires `heartbeat_timeout` seconds after the
   *last real progress*.
2. `heartbeat_timeout` is redefined as **"maximum acceptable time making zero
   forward progress"** — a roughly workload-independent number (minutes), unlike
   total duration. **Its default rises from 60s to 300s** in the same change that
   flips the semantics (see *Migration*): 60s under the new meaning would be a live
   tripwire that even well-instrumented apps trip between coarse-grained signals.
3. `start_to_close` becomes a **pure backstop** (e.g. 24h) that authors never tune.
4. Opaque single blocking calls that emit no progress (one long query/API call)
   are kept alive by an explicit, scoped **liveness bracket** that is **bounded by
   the blocking call's own declared timeout** — the DB `statement_timeout` / `httpx`
   timeout ADR-0010 already mandates. The bracket vouches for the call only until
   that bound elapses; past it, the beats stop and `heartbeat_timeout` fires. This
   keeps the "blocking code owns its timeout" contract *and* closes the hole where
   a wedged blocking call would otherwise be unguarded until the 24h backstop.

This shifts tuning off the hard knob (duration) onto two answerable ones: *"how
long is no progress acceptable?"* (uniform default) and *"how long should one
query take?"* (a property of the resource, which the call already declares).

### What counts as "progress"

Progress is defined operationally, not by intuition — this is the question a
reviewer asks first, so it must be crisp:

> **Progress = one observable unit of work completing** — a batch written, a chunk
> or file transferred, a page fetched, or an explicit `context.heartbeat()`.

It is emphatically **not** wall-clock time and **not** event-loop liveness — that
was the old unconditional keepalive this ADR removes. The single rule an author
needs follows directly from the definition:

> **If any one step can run longer than the no-progress budget (`heartbeat_timeout`,
> default 300s) without emitting a signal, that step must be made observable** — it
> is already covered by a framework hook, or the author adds a manual beat or a
> bracket.

By construction, **any gap between progress signals longer than `heartbeat_timeout`
is treated as a stall.** That is the intended behavior, not a corner case: it is
exactly how a wedged-but-quiet activity gets caught. The design's job is to ensure
the *legitimate* quiet spots are the small, known set that the automatic hooks and
the bracket already cover.

### The progress token

`TemporalHeartbeatController` (`execution/heartbeat.py`) gains a monotonic
progress generation and a blocking **deadline** (not just a depth counter — the
deadline is what bounds the bracket, see below):

```python
class TemporalHeartbeatController:
    def __init__(self) -> None:
        self._last_details: tuple[Any, ...] = ()
        self._progress_gen: int = 0            # bumped on every real progress signal
        self._blocking_deadlines: list[float] = []  # monotonic deadlines of in-flight blocking ops

    def mark_progress(self) -> None:
        """Framework + app signal for real forward progress."""
        self._progress_gen += 1

    def heartbeat(self, *details: Any) -> None:
        # A manual heartbeat is, by definition, progress.
        self._last_details = details
        self._progress_gen += 1
        activity.heartbeat(*details)

    def enter_blocking(self, timeout: float) -> None:
        # Vouch for a blocking op only until its own declared timeout elapses.
        self._blocking_deadlines.append(time.monotonic() + timeout)

    def exit_blocking(self) -> None:
        if self._blocking_deadlines:
            self._blocking_deadlines.pop()

    def blocking_active(self) -> bool:
        # A blocking op is vouched-for only while its deadline is in the future;
        # once a call overruns its own timeout we stop beating for it and let
        # heartbeat_timeout fire (the thread itself can't be killed — see below).
        now = time.monotonic()
        return any(d > now for d in self._blocking_deadlines)

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
        or hb.blocking_active()              # bounded opaque op still within its timeout
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

### Feeding the token (three mechanisms, most-automatic first)

These are the three observability mechanisms from the mental model, ordered by how
little the app author has to do:

1. **Framework hooks (automatic, no app action).** Call `hb.mark_progress()`
   wherever the SDK already loops over units of work: the batched output writers,
   the record/statistics emission path, and the `ObjectStore` transfer loops
   (`storage/transfer.py`, `storage/chunked.py`, `storage/file_ref_sync.py`). This
   covers the streaming majority — the bulk of connector runtime — with no code
   change in the app.
2. **The blocking bracket around `run_in_thread` (automatic for blocking work).**
   Anything offloaded through `run_in_thread` is bracketed by the SDK itself, so a
   legitimate long blocking call is never false-killed (details below).
3. **Manual `context.heartbeat(...)` or `holding_progress(...)` (explicit, app
   action).** The residual: a custom async loop, or an opaque single `await`
   against the connector's own source client (see the asymmetry below — this is
   *not* a rare case). `context.heartbeat()` already exists and now also bumps
   progress (still carrying resume details for `get_last_heartbeat_details()`);
   `holding_progress()` is the async analogue of the blocking bracket. This is the
   mechanism that asks the author to *do* something — and the audit step in
   *Rollout* exists to find exactly these spots.

### The blocking bracket — bounded by the call's own timeout

Two questions from review shape this design:

**Q: If my blocking code goes through `run_in_thread`, does it just beat forever?**
Not anymore. A naive "beat while `blocking_depth > 0`" would keep a *wedged*
blocking call alive until the 24h backstop — reintroducing the exact hole this ADR
closes for async code. Instead the bracket is **bounded by the blocking call's own
declared timeout** and stops vouching once that elapses:

```python
async def run_in_thread(func, *args, blocking_timeout: float | None = None, **kwargs):
    hb = _current_heartbeat_controller()
    bound = blocking_timeout if blocking_timeout is not None else _derive_timeout(func, kwargs)
    if hb and bound is not None:
        hb.enter_blocking(bound)
    try:
        return await loop.run_in_executor(_BLOCKING_EXECUTOR, ...)
    finally:
        if hb and bound is not None:
            hb.exit_blocking()
```

Effective kill time for a wedged blocking call therefore drops from ~24h to
`blocking_timeout + heartbeat_timeout`. (The Python thread itself cannot be
cancelled — that is `run_in_thread`'s known limitation; only `run_fault_isolated`
kills via a child process. The bracket doesn't kill the thread; it stops *lying to
Temporal* about it, so the slot is reclaimed and the activity fails/retries.)

**Where does the bound come from? Reuse the number the call already declares.**
ADR-0010 already mandates that blocking code owns a timeout
(`requests.get(url, timeout=30)`, `httpx` timeout, DB `statement_timeout`). The
bracket bound is *that* number — not a new one to invent, which keeps faith with
this ADR's whole thesis of eliminating guessed durations. Layered so we never
silently guess:

1. **Explicit is the contract:** `run_in_thread(func, ..., blocking_timeout=30)`.
2. **Convenience derivation** (`_derive_timeout`): if omitted, best-effort read a
   `timeout=` / `timeout_seconds=` kwarg off the call and reuse it, logging at
   DEBUG what was derived. Covers the common `requests`/`httpx` case with zero
   boilerplate — the author already typed the number once. *Caveats made explicit
   so no one over-trusts it:* it is heuristic — units vary (s vs ms), `requests`
   uses a `(connect, read)` tuple, and some callees don't name the arg `timeout`
   at all. Derivation is a convenience over the explicit arg, never a replacement.
3. **Conservative fallback, never 24h:** if no bound is found, cap at a small
   multiple of `heartbeat_timeout` and emit a WARNING, so the missing timeout
   surfaces during the opt-in audit rather than hiding until a wedge.

### The async escape hatch — and the asymmetry that makes it common

For an async-opaque single `await` with no progress signal, `holding_progress()` is
the explicit analogue of the blocking bracket:

```python
async with self.context.holding_progress("snapshot metadata query", timeout=120):
    rows = await long_single_query(...)   # its own statement_timeout is the watchdog
```

**Q: "Making people use it *always* might become a problem."** This is the most
important open ergonomics question, and we should not pretend it away. There is a
real asymmetry between the blocking and async paths:

- **Blocking source calls are auto-covered.** `run_in_thread` is a *mandatory
  seam* — every blocking call already goes through it (ADR-0010), so the SDK
  brackets it for the author with no extra code.
- **Async source calls have no equivalent mandatory seam.** A connector connects
  to its *own* source system with its *own* async client (an async SQLAlchemy
  engine, an `httpx.AsyncClient`, a vendor SDK). **The SDK's internal SQL/HTTP
  clients are for the SDK's own purposes; they do not, and are not meant to, sit on
  the connector→source path** — so there is no SDK-owned wrapper we can transparently
  instrument for that call. Auto-bracketing "the SDK clients" would cover almost
  none of the real source I/O.

Two things soften this, but neither eliminates it:

1. **Interleaved streaming reads are already covered by the write side.** The
   common extraction shape — fetch a page, write a batch, repeat — ticks
   `mark_progress()` on every batch write, so the read loop is covered as long as
   one fetch+write cycle stays under the no-progress budget. `holding_progress()`
   is *not* needed there.
2. The residual is the genuinely **opaque single async call** — one large metadata
   query, one slow list/export API call that returns everything at once. Those
   emit nothing until they complete.

So the honest expectation: **`holding_progress()` will very likely be needed in
almost every connector**, because almost every connector makes at least one such
opaque async call against its source. It is a *standard part of writing a
long-running async task*, not a rare escape hatch. We make that acceptable by (a)
keeping it a one-line context manager whose `timeout` is just the client timeout
the author already sets, (b) documenting it as expected rather than exceptional,
and (c) having the opt-in audit specifically hunt source-side opaque async calls. A
forgotten bracket false-kills at the tail (see *Migration*), so for such calls it
is **required**, not advisory.

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
- Streaming work and `run_in_thread` blocking work are covered automatically; the
  residual (custom async loops, raw opaque awaits) shrinks to a small, auditable
  set that needs one explicit observability call.
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
a single long opaque call. The chosen option keeps the manual beat as one of three
mechanisms but removes the tax on the common paths: framework hooks and the
`run_in_thread` bracket cover the streaming and blocking majority automatically, so
manual observability is the small, audited residual — not the whole burden.

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
| Wedged-but-looping async (the gap today) | only `start_to_close` (→ 24h under the naive proposal) | **killed at `heartbeat_timeout` after last progress** |
| Legit long opaque call | keepalive keeps it alive; bounded by `start_to_close` | bracket vouches for it until its declared timeout; resource timeout is the watchdog |
| **Wedged blocking call in `run_in_thread`** | keepalive beats forever → only `start_to_close` (24h) | **bracket stops at the call's timeout → killed at `blocking_timeout + heartbeat_timeout`** (thread orphaned; slot reclaimed) |
| `run_best_effort` child-process work (ADR-0017) | parent awaits; keepalive beats | parent awaits via `run_in_thread`/await → bracket keeps beating |

### What authors tune afterward

| Knob | Before | After |
| --- | --- | --- |
| `start_to_close` | the number everyone guesses wrong | 24h backstop, never tuned |
| `heartbeat_timeout` | "how often do I beat" (coupled to keepalive) | **"max no-progress window"** — default 300s, roughly app-independent |
| opaque-call bound | none / `start_to_close` | resource-level timeout (DB `statement_timeout`, `httpx` timeout), reused as the bracket bound |

## Migration & backward compatibility — "what if I do nothing?"

The first question every consumer asks: *if I don't touch my code, what changes?*
Precisely because two knobs are in play, the answer must separate them — one is
safe to ship on upgrade, the other is not.

**The progress-gating flag — safe, changes nothing on upgrade.** Progress-gating is
**flag-gated, default off** (see *Rollout*), and the framework `mark_progress()`
hooks are **inert while the flag is off** — they bump a counter nobody reads. So
merely bumping the SDK version produces no new "no forward progress" kill.

**The `start_to_close` default (600s → 24h) — this one DOES change behavior, and
must NOT ship at plain upgrade.** If the 24h default landed as part of the base
upgrade, here is what would happen to an app that does nothing:

- Apps that set `timeout_seconds` explicitly per task (including every app in the
  failure dashboard) are unaffected — the default never reaches them. Their
  StartToClose kills persist until they lower/remove those values and/or opt in.
- Apps **relying on the 600s default** would get a 24h ceiling *while still running
  the old unconditional keepalive* (because progress-gating is off). That is the
  exact unsafe pairing from *Context*: `heartbeat_timeout` cannot catch a wedge, so
  a **wedged-but-alive activity would squat its worker slot for up to 24h**, doing
  no real work — starving the ~100-slot pool and badly delaying legitimate work.

The 24h backstop is only safe **once the progress-aware watchdog is active for that
app.** Therefore the default bump is **coupled to the opt-in flip** (Rollout step 4
is per-app-gated, not a global default change at upgrade): you never get the 24h
ceiling without the minutes-scale watchdog that makes it safe. Shipping the ceiling
fleet-wide for fast StartToClose relief is tempting, but it buys relief for
default-relying apps at the cost of a fleet-wide 24h-wedge exposure window — so it
is explicitly rejected as the default path. **Flipping the flag (which brings both
halves together) is the migration action.**

**On opt-in, whether unchanged code survives depends on its shape.** This is the
sharp half — for the last two rows, do-nothing code *will* start hitting
no-progress kills, and that is the design working as intended (those paths emit
nothing the SDK can see, i.e. a healthy-but-quiet task it cannot distinguish from a
wedged one):

| Existing code shape | Outcome on opt-in | Author action |
| --- | --- | --- |
| Streaming through SDK batch writers / stats / `ObjectStore` transfer | Covered — hooks tick | none |
| Already calls `context.heartbeat()` | Covered — bumps progress | none |
| Opaque blocking call via `run_in_thread` | Covered — bracket vouches to its timeout | none (set `blocking_timeout` if not derivable) |
| **Custom async loop doing its own I/O, never through an instrumented SDK path** | **False-killed** if any gap > `heartbeat_timeout` | add a beat/bracket |
| **Opaque single `await` against the source (connector's own async client)** | **False-killed** at the tail | wrap in `holding_progress()` — expected in ~every connector |

Two consequences fall out of this and are load-bearing for a safe rollout:

1. **The `heartbeat_timeout` default must move to 300s in lockstep with the
   semantic flip.** Apps that never touched it — left it at the old 60s — are the
   *most* exposed, not the least: under the new meaning, 60s of no SDK-observable
   signal (one coarse batch, one slow page) would trip even a fully instrumented
   app. The bump is not optional polish; it is part of the semantic change.
2. **Opt-in verification must exercise a large-tenant / tail profile, not a smoke
   test.** A small tenant's fast steps hide the very gaps that only open at the
   tail — the same tail that produced the original `StartToClose` failures. Signing
   off on a small run and flipping the flag would just relocate the tail failure.

## Rollout

Progress-gating cannot be turned on globally in one step: any existing task doing
pure-async work that never hits a framework hook or manual beat would start being
killed at `heartbeat_timeout`. Sequence:

1. **Ship the framework `mark_progress()` hooks first** (writers, emission,
   transfer loops) so the streaming majority is covered before anyone opts in.
2. **Gate behind a flag** — `progress_aware_heartbeat` on `@task` /
   `TaskMetadata`, defaulting **off**, plus an env kill-switch.
3. **Flip per-app after verification** — with hooks in place, the migration work is
   auditing the two at-risk shapes from *Migration* (custom async loops, opaque
   source awaits) and adding a beat/bracket, and verifying against a
   **large-tenant / tail profile**. The `heartbeat_timeout` default moves to 300s as
   part of this flip so its new meaning and its value change together.
4. **Couple the 24h `start_to_close` backstop to that same per-app flip** — an app
   gets the 24h ceiling *and* the progress-aware watchdog together, never one
   without the other. The **global** default only changes at the very end, once the
   flag is retired and progress-aware is universal; raising it fleet-wide any
   earlier would expose default-relying, non-opted apps to 24h wedges (see
   *Migration*). At that point the per-task duration constants are retired.

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
