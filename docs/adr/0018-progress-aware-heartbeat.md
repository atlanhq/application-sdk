# ADR-0018: Progress-Aware Stall Watchdog and Duration-Backstop Timeouts

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

The two timeouts catch *different* failure modes and neither subsumes the other:

- `heartbeat_timeout` detects **nothing is beating** — process crash, OOM kill,
  node loss, network partition, event-loop starvation. It fires ~`heartbeat_timeout`
  after the worker stops being able to beat.
- `start_to_close` detects **taking too long while beating**. It is the *only*
  bound on a healthy-but-slow or wedged-but-alive attempt.

### Problem 1: `start_to_close` is an unguessable number

`start_to_close` forces app authors to answer *"how long should this whole
activity take?"* — a number that scales with tenant size from seconds to days and
is data-dependent. In practice authors guess it, weight-class it, and still guess
wrong at the tail. A production failure dashboard (13-day window) showed a steady
trickle of `activity StartToClose timeout` failures across multiple connectors —
extraction, query-history, and processing activities — despite each app having
already tuned per-task timeouts (e.g. 5-minute "light", 2-hour "medium", 6-hour
"heavy" classes). The tuning is educated guessing, and the tail always exceeds it.

### Problem 2: heartbeat timeouts are the *larger* failure mode today

This ADR must be honest that `StartToClose` is the smaller half of the evidence.
Per FND-165, the `activity Heartbeat timeout` pattern re-alerted **18× since
Aug 3**, hitting **five connectors/tenants in a trailing 24h**, and **at least
nine teams** have independently re-tuned a heartbeat or timeout number since March
rather than fixing the mechanism (CONNECT-728 ThoughtSpot, CONNECT-141 dbt,
SHA-838/1318 publish, CAS-61/62 Context Agents — which needed **30 minutes**,
SHA-327 QI, LH-1097 Lakehouse).

Those heartbeat timeouts are, today, overwhelmingly *event-loop starvation*: the
auto-loop cannot run because blocking work is holding the loop, so no beat is sent
even though the activity is healthy. That is an ADR-0010 adoption problem, not a
timeout-semantics problem, and **it is not what this ADR fixes.** It matters here
for sequencing: any design that puts more weight on the heartbeat mechanism lands
on top of a mechanism that is already the top failure. See *Rollout*.

### Why a large `start_to_close` is unsafe today

The auto-heartbeat is an **unconditional keepalive**: `heartbeat_keepalive()`
calls `activity.heartbeat()` every 10s regardless of whether the activity is
making any real progress. It proves the *event loop is alive*, not that *work is
advancing*. Consequences:

1. Nothing catches a **wedged-but-alive** activity (an infinite async retry loop,
   a fetch that streams forever, a driver stuck in a yield-friendly wait).
   Heartbeats keep arriving, so only `start_to_close` ever stops it.
2. Therefore, if we raise `start_to_close` to 24h and change nothing else, a
   wedged activity is guarded by **nothing** for a full day → worker-slot
   exhaustion (Temporal's Python worker defaults to ~100 concurrent activity
   slots; a handful of stuck activities can starve the pool) and day-long
   non-detection.

The number that is genuinely hard to guess (total duration) cannot simply be made
huge unless *something else* reliably kills a stuck activity fast. Today nothing
does.

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
  hold. This is not a rare edge: because the async source path has no mandatory
  SDK seam (unlike blocking calls, which all go through `run_in_thread`), the
  explicit hold is expected in nearly every connector (see *The async escape
  hatch* below).

This framing matters for expectations: **the residual work never disappears.** The
ADR shrinks it to a small, auditable set and automates the common paths, but a task
that does long work through code the SDK can't see will still need one of the three
mechanisms. "Zero effort" is true only for the covered paths; everything else is a
deliberate, one-line observability choice.

### The watchdog runs in-process, and `heartbeat_timeout` keeps its meaning

Given a progress signal, the obvious move is to make the heartbeat *conditional* —
beat only on progress, and let Temporal's `heartbeat_timeout` do the killing. This
ADR deliberately **does not** do that (see *Option 5*, rejected). Instead:

1. The auto-heartbeat loop keeps sending its **unconditional** keepalive.
   `heartbeat_timeout` keeps its current meaning and its current 60s default: it
   detects *nothing is beating* — crash, OOM kill, node loss, partition,
   event-loop starvation.
2. The same loop gains a **stall watchdog**: it tracks a progress token, and when
   the token has been flat for **`max_no_progress_seconds`** it **fails the
   activity itself, in-process**, with a typed error naming the last progress
   signal and the elapsed stall.
3. `start_to_close` becomes a **pure backstop** (e.g. 24h) that authors never tune.
4. Long single operations that emit no progress — one big query, one slow API call,
   blocking or async — are covered by an explicit scoped **hold**
   (`holding_progress()`), which vouches for them for a duration the author
   declares. The SDK never derives or invents that duration (see *Holds*).

Why one operator concept and two enforcement points: *"it stopped doing anything"*
is a single idea, and the operator should see a single failure story. But the two
underlying situations want very different reaction times, and one number would have
to be the slower of the two:

- Nothing will ever beat again (OOM, SIGKILL, node loss): the attempt is already
  dead. Reclaim and retry **now** — 60s.
- Wedged-but-alive, or healthy-but-quiet: a legitimate quiet spell must be waited
  out first — **minutes**.

Collapsing these into one knob means a crash-killed worker's activities sit idle
for the whole no-progress budget before Temporal reclaims them. Graceful evictions
(KEDA scale-down, VPA, spot reclaim) don't pay that cost — the SIGTERM path
(`WORKER_EVICTED_TYPE`, `execution/_temporal/eviction_retry.py`) fires immediately
— but SIGKILL/OOM/node-loss has no path other than `heartbeat_timeout`. OOM is a
leading failure mode for exactly these connectors, so keeping 60s crash detection
is not conservatism; it is the point.

Keeping the two enforcement points separate also means the mechanisms cover each
other's blind spot exactly: the in-process watchdog cannot fire when the event loop
is starved — and that is precisely when `heartbeat_timeout` does.

This shifts tuning off the hard knob (duration) onto two answerable ones: *"how
long is no progress acceptable?"* (roughly workload-independent, one default) and
*"how long should this one operation take?"* (declared at the one call site that
knows).

### What counts as "progress"

Progress is defined operationally, not by intuition — this is the question a
reviewer asks first, so it must be crisp:

> **Progress = one observable unit of work completing** — a batch written, a chunk
> or file transferred, a page fetched, or an explicit `context.heartbeat()`.

It is emphatically **not** wall-clock time and **not** event-loop liveness — that
is what the unconditional keepalive already proves, and it is why the keepalive
cannot double as the stall detector. The single rule an author needs follows
directly from the definition:

> **If any one step can run longer than the no-progress budget
> (`max_no_progress_seconds`) without emitting a signal, that step must be made
> observable** — it is already covered by a framework hook, or the author adds a
> manual beat or a hold.

By construction, **any gap between progress signals longer than the budget is
treated as a stall.** That is the intended behavior, not a corner case: it is
exactly how a wedged-but-quiet activity gets caught. The design's job is to ensure
the *legitimate* quiet spots are the small, known set that the automatic hooks and
holds already cover.

### The progress tracker

Progress tracking is a **new object, not a change to `HeartbeatController`.** The
controller's job (send beats to Temporal) is unchanged, so its Protocol and
`NoopHeartbeatController` keep their current contract. The tracker has no Temporal
dependency, which means local and test execution exercise the same code path as
production — and, usefully, a task with `heartbeat_timeout_seconds=None` still gets
a stall watchdog.

```python
@dataclass(frozen=True)
class _Hold:
    label: str
    started_at: float
    deadline: float | None   # None = unbounded


class ProgressTracker:
    """Observed forward progress for one activity attempt."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        # Injected clock, not a patched global: an asyncio loop shares
        # time.monotonic, so patching it globally in tests makes the loop itself
        # misbehave.
        self._clock = clock
        self._last_label: str = ""
        self._last_at: float = clock()
        self._holds: dict[int, _Hold] = {}
        self._next_token: int = 0

    def mark_progress(self, label: str = "") -> None:
        """Framework + app signal for real forward progress."""
        self._last_at = self._clock()
        if label:
            self._last_label = label

    def enter_hold(self, label: str, timeout: float | None) -> int:
        """Vouch for an in-flight opaque operation. Returns a token."""
        token = self._next_token
        self._next_token += 1
        now = self._clock()
        self._holds[token] = _Hold(
            label, now, None if timeout is None else now + timeout
        )
        return token

    def exit_hold(self, token: int) -> None:
        # Keyed by token, not popped off a stack: concurrent holds in one
        # activity (asyncio.gather over several run_in_thread calls) must not
        # release each other's deadlines.
        hold = self._holds.pop(token, None)
        if hold is not None:
            # A completed operation *is* progress. The observed duration also
            # feeds the warn-mode report: a long *unbounded* hold is a site that
            # wants an explicit allowance (see Warn mode).
            _observe_hold_closed(hold, duration=self._clock() - hold.started_at)
            self.mark_progress(hold.label)

    def held(self) -> bool:
        now = self._clock()
        return any(
            h.deadline is None or h.deadline > now for h in self._holds.values()
        )

    def stalled_for(self) -> float:
        """Seconds since the last observable progress, 0 while vouched-for."""
        return 0.0 if self.held() else self._clock() - self._last_at
```

### The watchdog in the loop

The existing send, event-loop-block warning and memory sampling are unchanged. The
watchdog is additive:

```python
while not stop_event.is_set():
    ...  # existing wait_for(stop_event) + block-detection warning + memory sampling

    hb.heartbeat_keepalive()          # unchanged, unconditional: the crash detector

    stalled = progress.stalled_for()
    if budget_seconds is not None and stalled >= budget_seconds:
        # Always a metric; the log level differs by mode. In warn mode this is an
        # expected observation on a fleet-wide default, so it must NOT be WARNING
        # — see "Warn mode is the default".
        _no_progress_gap.record(stalled, task=task_name, label=progress.last_label)
        if mode is Mode.ENFORCE:
            logger.warning(
                "Task '%s' made no observable progress for %.0fs (budget %.0fs); "
                "last signal was '%s' — failing the activity",
                task_name, stalled, budget_seconds, progress.last_label or "<none>",
            )
            on_stall(stalled, progress.last_label)
            return   # cancel-and-return: the activity's finally awaits this task
        logger.info(
            "Task '%s' made no observable progress for %.0fs (budget %.0fs); last "
            "signal was '%s' — not failing (warn mode)",
            task_name, stalled, budget_seconds, progress.last_label or "<none>",
        )
        progress.mark_progress()   # re-arm so warn mode reports each gap once
```

`on_stall` is injected by `activities.py`, the same way `heartbeat_fn` already is,
so `heartbeat.py` stays free of activity/Temporal semantics and the watchdog is
unit-testable without a worker.

### Warn mode: the watchdog is its own audit tool

The obvious question from any app author is *"how do I find every place in my
codebase that needs a hold, and what allowance do I put on each one?"* Answering
that with "audit your code" would reintroduce this ADR's own problem one level down:
a per-site number, guessed up front, by hand.

So the flag has **three states, not two** — `off`, `warn`, `enforce`:

- **`off`** — hooks are inert, nothing observes anything. Byte-identical to today.
  Retained as a kill-switch, not as the normal state.
- **`warn`** — the watchdog runs and reports, but can never fail an activity. This is
  the audit tool, and the **default**.
- **`enforce`** — as described above.

Warn mode emits the two things an author needs, and they map to the two shapes that
need action:

1. **No-progress gaps with no hold in force** — task, last progress signal, gap
   duration. These are the sites that need a beat or a hold.
2. **Long *unbounded* holds** — recorded by `exit_hold` above. Without this, a
   blocking call through `run_in_thread` would be invisible to the audit precisely
   because it is auto-vouched-for; these are the sites that want an explicit
   allowance instead of relying on the backstop.

A run against a large tenant therefore produces a ranked work-list, and the author
wraps the sites that exceeded the budget and ignores the rest — which is most of the
app. The whole-codebase audit collapses into reading a report.

#### Warn mode is the default, fleet-wide

Because warn mode cannot fail an activity, there is no reason to make apps opt into
it — and two reasons not to:

- **It removes a per-app coordination step from the critical path.** Asking ~20
  connector teams to each turn a flag on is exactly the shape of work that FND-165
  documents stalling (nine teams independently re-tuning a number over five months).
  Defaulting to `warn` means the evidence arrives without anyone being asked to care.
- **It makes the migration an *eventual optimisation* rather than an upgrade task.**
  On upgrade an app does nothing, and keeps doing nothing until someone chooses to
  look at its report. `holding_progress()` becomes work you do when you want the
  stronger guarantee, not a precondition for taking the version.

Two constraints come with defaulting it on:

- **Warn-mode findings must not be WARNING-level.** A stall observation in warn mode
  is an expected observation on a fleet-wide default, not an actionable failure.
  Emitting it at WARNING would manufacture fleet-wide alert noise — the same class of
  noise this ADR is trying to reduce. It is a metric plus an INFO log; dashboards and
  the per-app report read the metric.
- **Hooks go on batch, chunk and page boundaries — never per record.** `mark_progress()`
  is cheap, but "cheap" in a per-record loop is still a hot-path cost for no extra
  signal: one batch is already the unit of observable work.

"No behaviour change on upgrade" is therefore precise rather than absolute: no
activity fails differently, no knob changes meaning, and the added cost is telemetry.
`off` exists for anyone who needs even that gone.

The same telemetry settles every sizing parameter at once — where holds go, what each
allowance should be, what `max_no_progress_seconds` should be, and whether long stalls
occur in practice at all (currently argued from recollection on both sides). None of
those are design questions to be decided in review; see *What this needs agreement on*.

### Failing the activity, and what the failure looks like

`activities.py` captures `asyncio.current_task()` before it creates the heartbeat
task (`activities.py:196-207`), and `on_stall` flags the tracker and cancels that
task. The cancellation lands at the activity's next `await` and hits the existing
`except asyncio.CancelledError` handler (`activities.py:259`).

That handler gains a branch **ahead of** the `is_worker_shutting_down()` check,
which raises a typed `AppError` — a new `TaskStalledError` alongside
`AppTimeoutError` in `errors/leaves.py` — carrying `stalled_for_seconds` and
`last_progress_label` as dataclass fields, so redaction stays at the
`FailureDetails` wire layer rather than being achieved by withholding context.

Three details that matter:

- **Flag-check order.** A stall and a SIGTERM can coincide. The stall branch must
  come first, or a stalled activity is mislabelled `WorkerEvicted` and re-dispatched
  by the eviction-retry loop (`app/base.py:2447`) *outside* the normal retry budget.
- **Cancel and return.** The watchdog *is* `heartbeat_task`, and the activity's
  `finally` (`activities.py:334-347`) sets `stop_event` and awaits it with a 1s
  bound. The watchdog must return immediately after cancelling, not keep looping.
- **Translation is shared, not duplicated.** The `AppError → ApplicationError(*FailureDetails)`
  translation in the `except Exception` branch (`activities.py:299-331`) already
  handles the non-serialisable-evidence guard, `_sever_cause_chain` (BLDX-1512) and
  `non_retryable=not e.effective_retryable`. Factor it into a helper both branches
  call.

**`TaskStalledError` is retryable.** The instinct is to make it non-retryable — a
wedge re-wedges, and without checkpointing (REQ-1609) a retry restarts from zero. That
instinct is wrong about what a stall usually *is*. Sort the causes:

| Cause of the stall | Retry outcome |
| --- | --- |
| Source-side hang: dropped connection, source overloaded, socket read with no timeout, exhausted pool | **Usually succeeds.** The likely majority: apps whose error handling never surfaced a transient source problem, so the symptom is silence rather than an exception. |
| Genuine code wedge: infinite retry loop, app-level deadlock | Re-stalls, costing up to 3 × `max_no_progress_seconds` (minutes) — not 3 × the backstop |
| Timing-dependent deadlock on a shared resource | May well succeed on different timing |
| Missing instrumentation (healthy, quiet, no hold declared) | **False kill, amplified** — retries waste the same partial work again |

The dominant category is transient and self-heals on retry, and the failure the fleet
actually sees today is exactly that shape: a source stops responding and the app has no
way to say so. Refusing to retry would convert those into failed runs needing a manual
re-run — which restarts from zero anyway, just slower and with a human in the loop. So
non-retryable buys nothing there and costs a lot.

That leaves the last row as the real risk, and it is bounded by process rather than by
policy: eliminating missing instrumentation is precisely what the warn-mode pass does
before an app enforces.

A refinement worth considering later, not for v1: a stall *inside a lapsed hold* means
the source exceeded an allowance a human declared for it — almost definitionally the
transient case — whereas a stall with no hold at all is likelier to be missing
instrumentation. The two could carry different retry policies.

Detection latency is `max_no_progress_seconds` plus at most one loop tick (10s).

### Feeding the tracker (three mechanisms, most-automatic first)

1. **Framework hooks (automatic, no app action).** Call `mark_progress()` wherever
   the SDK already loops over units of work: the batched output writers, the
   record/statistics emission path, and the `ObjectStore` transfer loops
   (`storage/transfer.py`, `storage/chunked.py`, `storage/file_ref_sync.py`). This
   covers the streaming majority — the bulk of connector runtime — with no code
   change in the app.
2. **`run_in_thread` auto-holds (automatic for blocking work).** Anything offloaded
   through `run_in_thread` is wrapped in an **unbounded** hold by the SDK, so a
   legitimate long blocking call is never false-killed and behaviour on upgrade is
   unchanged. An unbounded hold means the stall watchdog is inactive for the
   duration of the call and the 24h backstop is the only bound — a real, documented
   residual. Warn mode surfaces each one with its observed duration, and the
   conformance rule keeps them visible afterwards.
3. **Manual `context.heartbeat(...)` or `holding_progress(...)` (explicit, app
   action).** The residual: a custom async loop, or an opaque single `await`
   against the connector's own source client (see the asymmetry below — this is
   *not* a rare case). `context.heartbeat()` already exists and now also marks
   progress (still carrying resume details for `get_last_heartbeat_details()`).

The tracker reaches `run_in_thread` and `holding_progress` through a ContextVar set
by `activities.py`. There is no such ContextVar today — the controller is passed
into `TaskExecutionContext` — so this is new plumbing, and it must cover the
module-level `run_in_thread` (`execution/heartbeat.py:234`), not only the
`TaskExecutionContext` wrapper, since both are used.

### Holds: the SDK never invents a duration

One mechanism covers both blocking and async opaque work:

```python
# async: the connector's own client
async with self.context.holding_progress("snapshot metadata query", timeout=1800):
    rows = await long_single_query(...)

# blocking: the same wrapper around the offload
async with self.context.holding_progress("full table scan", timeout=7200):
    rows = await self.context.run_in_thread(cursor.execute, sql)
```

An earlier draft of this ADR proposed *deriving* the hold's bound from the
`timeout=` kwarg the call already carries, on the theory that ADR-0010 already
mandates that number and reusing it avoids inventing a new one. **That is rejected**,
for two reasons:

1. **`timeout=` almost never means "wall-clock bound on this call."** It is
   per-operation: `requests`' read timeout bounds the gap between socket reads (a
   streaming download legitimately runs for hours without exceeding it), `httpx`
   is the same shape, DB `statement_timeout` is per *statement* (a call issuing
   forty statements is bounded at forty times the limit), socket timeouts are per
   recv. A successfully derived number is therefore systematically *smaller* than
   the call's legitimate duration, so bracketing on it false-kills healthy work.
2. **A derived bound is a total-duration guess wearing a disguise** — the exact
   knob this ADR exists to abolish, reintroduced for blocking calls.

The dominant real shape makes it worse: `run_in_thread(cursor.execute, sql)` has
no `timeout=` kwarg at all (the bound lives on the connection), so derivation
fails and a fallback takes over. Any fallback tight enough to be useful as a
watchdog — "a small multiple of the no-progress budget" — is *tighter than the
status quo*, where the same call is bounded by the task's `start_to_close` of 2h or
6h. Opt-in would then make legitimate long blocking work fail sooner than before,
in precisely the apps most likely to opt in.

(As a mechanical note, `blocking_timeout=` could not have been a `run_in_thread`
kwarg anyway: `run_in_thread(func, *args, **kwargs)` forwards every kwarg to
`func`, so the name would collide. The context manager sidesteps that too.)

So there are exactly two honest paths at a call the SDK cannot see into, both
explicit and both greppable:

- The author declares a real wall-clock allowance and `holding_progress` honours it.
  Past it, the hold lapses, the watchdog resumes, and the stall fires
  `max_no_progress_seconds` later. Effective kill time for a wedged call is
  `timeout + budget` instead of 24h.
- The author declares nothing, the hold is unbounded, and the 24h backstop owns it —
  an accepted residual, surfaced by the conformance rule rather than hidden.

Opaque operations are the one place a duration bound is genuinely unavoidable,
because there is nothing to observe from outside a blocked thread. The ADR's
position is to say so, and to let a human state the number at the one site that
knows it — never to let the SDK guess it.

### Choosing the allowance

Asking an author for a number is only defensible if the question is answerable, so
it has to be framed precisely. The allowance is **not** a prediction of how long the
operation takes. It is *how long you would let this one operation run before you
would rather it failed.* That is answerable at the call site in a way that "how long
should this whole activity take?" never was, because it is a property of one
operation against one resource rather than of a tenant's data volume.

Two things make it safe to answer:

- **Warn mode gives you the evidence.** Use the observed p99 for that site plus
  headroom, rather than a number invented at the desk.
- **The error is asymmetric, so err generous.** Too generous only delays detection
  toward the backstop — mildly worse observability. Too tight kills a healthy run, and
  because stall kills retry, a too-tight allowance burns the same wasted work up to
  three times before failing. Anyone unsure should round up.

### The async escape hatch — and the asymmetry that makes it common

**Q: "Making people use it *always* might become a problem."** This is the most
important open ergonomics question, and we should not pretend it away. There is a
real asymmetry between the blocking and async paths:

- **Blocking source calls are auto-held.** `run_in_thread` is a *mandatory seam* —
  every blocking call already goes through it (ADR-0010), so the SDK holds for the
  author with no extra code.
- **Async source calls have no equivalent mandatory seam.** A connector connects
  to its *own* source system with its *own* async client (an async SQLAlchemy
  engine, an `httpx.AsyncClient`, a vendor SDK). **The SDK's internal SQL/HTTP
  clients are for the SDK's own purposes; they do not, and are not meant to, sit on
  the connector→source path** — so there is no SDK-owned wrapper we can transparently
  instrument for that call. Auto-holding "the SDK clients" would cover almost none
  of the real source I/O.

Two things soften this, but neither eliminates it:

1. **Interleaved streaming reads are already covered by the write side.** The
   common extraction shape — fetch a page, write a batch, repeat — marks progress
   on every batch write, so the read loop is covered as long as one fetch+write
   cycle stays under the budget. `holding_progress()` is *not* needed there.
2. The residual is the genuinely **opaque single async call** — one large metadata
   query, one slow list/export API call that returns everything at once. Those
   emit nothing until they complete.

So the honest expectation: **`holding_progress()` will very likely be needed in
almost every connector**, because almost every connector makes at least one such
opaque async call against its source. It is a *standard part of writing a
long-running async task*, not a rare escape hatch. We make that acceptable by (a)
keeping it a one-line context manager, (b) documenting it as expected rather than
exceptional, and (c) never asking anyone to find these by reading code — warn mode
names the sites and sizes their allowances, and the conformance rule keeps them
visible afterwards. A forgotten hold false-kills at the tail (see *Migration*), so
for such calls it is **required**, not advisory.

## Bounding total time

Removing the duration knob removes the only wall-clock bound the system has, and
the ADR must say what replaces it — "nothing" is not an answer that survives an
incident.

**Retries multiply the backstop, and we are accepting that product as-is to start.**
`get_activity_options` (`execution/_temporal/activities.py:418-420`) returns only
`start_to_close_timeout` and a retry policy — **no `schedule_to_close`**. With the
default `retry_max_attempts=3`, a 24h backstop is a **72h worst case per activity,
where today that product is 30 minutes.** A `StartToClose` timeout is retryable in
Temporal, so a wedge that survives to the ceiling three times really does consume the
full 72h.

**Decision: start there deliberately, rather than bounding it up front.** The number
looks alarming in isolation, and is defensible for three reasons:

1. **Nobody is blind for 72h.** From the first release the stall metric makes a wedge
   visible within `max_no_progress_seconds` and alerts on it. The 72h is the outer
   bound on *automatic* termination, not on *detection* — and it replaces a world where
   a wedge is never diagnosed at all, only accidentally killed by a small ceiling.
2. **The product collapses once an app enforces.** An enforcing app's wedge is caught
   at `max_no_progress_seconds` per attempt, so the worst case becomes 3 × minutes
   rather than 3 × 24h — roughly 45 minutes at a 900s budget. The 72h is the warn-mode
   worst case, and it shrinks by three orders of magnitude as apps enforce.
3. **Bounding it now would be another guessed number.** Picking a `schedule_to_close`
   budget today means guessing a total duration — the exact move this ADR exists to
   remove. The warn-mode data gives a real distribution to size it from, if it turns
   out to need sizing at all.

If the data shows the product matters, the fix is already scoped: set
`schedule_to_close` as the real backstop and let `start_to_close` bound one attempt,
following the budget-shaped pair the SDK already uses in `gate_timeouts`
(`app/base.py:1902`), or cap attempts at 1 for backstop-class tasks.

**Retries without checkpointing are expensive, but not uniquely so.** Without
REQ-1609 (activity-level checkpointing), a stall kill at hour 5 of a 6-hour extraction
restarts from zero. The watchdog makes failure *faster to detect* without making it
*cheaper to recover*. This is the main reason the tail-profile verification in *Rollout*
is a gate rather than a suggestion.

It is worth being precise about what checkpointing does and does not gate, though.
Restarting from zero is what already happens on every `StartToClose` retry today, and
it is also what happens when a human re-runs a failed workflow — so it is not a cost
this ADR introduces, and it is not an argument for refusing to retry stalls (see
*Failing the activity*). REQ-1609 makes both the existing and the new paths cheaper.
That downgrades it from a correctness gate to an efficiency one.

**A run with no duration bound anywhere still needs a duration signal.** Once the
AE layer drops its timeouts (below) and `start_to_close` is a 24h backstop, a run
that wedges while dribbling small amounts of progress is effectively unbounded. The
replacement for a duration *kill* is a duration *alert* — an SLA on run length in
the observability stack — so "remove the timeouts" does not quietly become "nobody
notices for a week."

## The Automation Engine layer

An earlier draft declared the AE-orchestrated DAG-node timeouts out of scope. That
is wrong in effect: the failures operators actually report — a connector run killed
at exactly 2h — come from *that* layer, not from `@task`. Scoping it out while the
symptom lives there makes this ADR look like a fix for something it does not touch.

**AE should hold no duration opinion at all.** Connector DAG nodes are child-workflow
invocations, not activities: `DAGNode.nodeType` defaults to `"workflow"`
(`contract-toolkit/src/App.pkl:686`) because `renderNode` always emits
`activity_name = "execute_workflow"`, and the toolkit *rejects*
`heartbeatTimeoutSeconds` on workflow nodes (`App.pkl:716-720`) precisely because
it is activity-only. Temporal requires no timeout on a workflow —
`workflow_execution_timeout` and `workflow_run_timeout` both default to unlimited;
only `workflow_task_timeout` has a mandatory default, and that bounds task dispatch,
not run duration. So there is nothing structurally forcing a number, and the
unguessability argument applies with full force one layer up.

Three pieces of work follow, none of which are `@task` changes:

1. **AE drops its default.** The toolkit cannot fix this by omission: setting
   `errorHandling = null` falls back to *AE's own source default of 2h*
   (`App.pkl:706-714`) — the number the toolkit already raised to 1d/72h to work
   around. AE has to stop applying a duration to workflow nodes.
2. **The toolkit floors the override.** `startToCloseTimeoutSeconds` is currently
   `Int(isBetween(1, 864000))` (`App.pkl:740`) — a cap with no floor, which is how
   an app ships 7200. Give it a floor so an app cannot tune *below* the generous
   default.
3. **A conformance rule makes it permanent.** A rule that flags an app-level node
   timeout below the floor runs across every connector repo in seconds, reports
   through connector-pulse, and is a regression guard forever — rather than a
   one-off sweep repeated by hand each time this resurfaces. The same rule carries
   the `@task` side: opaque `run_in_thread` / source-await sites with no declared
   hold (see *Holds*).

With all three, the picture is consistent at every layer: **no layer holds a
duration opinion; the only watchdog is progress, inside the app; duration is an
alert, not a kill.**

## Options Considered

### Option 1: Raise the `start_to_close` default to 24h and delete per-task tuning (Rejected as-is)

The originating proposal: stop guessing durations by making the default enormous
and removing the per-task constants.

**Why rejected on its own:** with today's unconditional keepalive and no stall
watchdog, nothing kills a wedged-but-alive activity for 24h → worker-slot
exhaustion and day-long non-detection (see Context). It also does not even fix the
observed failures: the apps in the dashboard *already* override the default on
every task, so bumping the default reaches none of them. This option is the
*cosmetic half* of the real fix and is only safe once Option 3 is in place.

### Option 2: Keep guessing, tune per-task durations harder (Rejected)

Continue the status quo — better per-task `timeout_seconds` guesses.

**Cons:** the number is fundamentally unguessable (scales with tenant/data); the
dashboard shows thoughtful guesses still failing at the tail; FND-165 documents
nine teams doing this independently without convergence. Unbounded tuning that
never converges.

### Option 3: In-process stall watchdog + duration backstop (Chosen)

Keep the unconditional keepalive and `heartbeat_timeout` as-is; add a progress
tracker and an in-process watchdog on its own budget; make `start_to_close` a
backstop. Detailed above.

**Pros:**
- Eliminates the unguessable knob; the surviving knobs don't scale with tenant size.
- Kills wedged activities in minutes instead of a day → no slot exhaustion.
- Crash/OOM reclaim stays at 60s.
- No semantic change to an existing knob, so no "same field, new meaning"
  migration and no coupled default bump.
- The failure is a typed SDK error naming the last progress signal, not Temporal's
  generic timeout string — which is also what makes the rollout measurable.
- Streaming work and `run_in_thread` blocking work keep working with no code
  changes; the residual shrinks to a small, conformance-auditable set.

**Cons:**
- Cancelling the activity task from a sibling task technically violates asyncio's
  cancellation protocol — the same trade-off, with the same rationale, that
  `activities.py:259-291` already documents for the worker-shutdown path.
- A CPU-bound wedge with no `await` cannot receive the cancellation (covered by
  `heartbeat_timeout`, which is exactly why it is kept).
- A wedged blocking thread is still not killable — the hold lapses, the slot is
  reclaimed, the thread is orphaned. Unchanged from today; `run_fault_isolated` is
  the only tool that kills.
- Requires framework hooks at the right loops and an audit of opaque calls
  (migration cost).

### Option 4: Manual heartbeating only (Rejected)

Disable auto-heartbeat; require `context.heartbeat()` everywhere. Rejected for the
same reasons as ADR-0010 Option 3 — easy to forget, and it cannot heartbeat during
a single long opaque call. The chosen option keeps the manual beat as one of three
mechanisms but removes the tax on the common paths.

### Option 5: Progress-gate the heartbeat and let `heartbeat_timeout` kill (Rejected)

The most tempting variant, and the one an earlier draft of this ADR chose: send a
beat only when the progress token advanced, redefine `heartbeat_timeout` as "max
acceptable time making zero forward progress," and raise its default from 60s to
300s in the same change.

It does work mechanically. Temporal's Rust Core spawns local heartbeat and
`start_to_close` timers per activity task specifically so that "activities can still
get cleaned up even if the user isn't heartbeating"
(`sdk-core/src/worker/activities.rs`), issuing a cancel with reason
`ActivityCancelReason::TimedOut`. That behaviour is present across the SDK's
declared `temporalio>=1.25.0` floor (verified in 1.23 and 1.30), so withholding
beats really does get the activity cancelled and the slot reclaimed — this is not
the reason it is rejected.

**Why rejected:**

- **It spends crash-recovery latency to buy the semantic merge.** One knob must be
  sized for the *slower* case, so raising it to 300s (or the 1800s some apps have
  needed) is also raising the time an OOM-killed worker's activities sit
  unreclaimed, from 60s to the whole budget. OOM is a leading failure mode for
  these connectors.
- **It loads more weight onto the mechanism that is already the top failure.**
  Heartbeat timeout is the dominant production alert (Problem 2). Progress-gating
  adds a second, independent way to trip the same timeout, and the resulting
  failure is indistinguishable from the existing loop-starvation one — precisely
  when CNCT-10 is trying to split timeout subtypes apart.
- **The kill produces a bare `CancelledError`.** `activities.py:259` only
  translates cancellation for worker shutdown, so a stall would surface with no
  explanation, no last-progress context, and no way to measure the rollout.
- **The migration is riskier for no gain.** Redefining a live knob's meaning forces
  a coupled default bump, and apps that never touched `heartbeat_timeout` are the
  *most* exposed rather than the least. An in-process watchdog on its own new knob
  needs neither.
- **It silently changes what existing constraints mean.** The toolkit caps
  `heartbeatTimeoutSeconds` at 3600 and asserts `heartbeat < startToClose`
  (`App.pkl:742-748`); both were written against the current meaning.

Under Option 3, Core's local timers remain a useful backstop-of-the-backstop for
the case the in-process watchdog cannot cover (a starved loop), which is exactly
the division of labour we want.

## Rationale

### The asymmetry that makes the inversion work

*"How long should the whole thing take?"* is workload-specific and unbounded.
*"How long is it acceptable to make zero forward progress?"* is roughly
workload-independent — a few minutes for almost any connector. One sane global
number can express the second; no global number can express the first. That
asymmetry is the entire reason a large `start_to_close` default becomes safe once a
stall watchdog exists.

### Failure detection is preserved or improved for every mode

| Scenario | Before | After |
| --- | --- | --- |
| Worker OOM-killed / node lost / partition | killed at `heartbeat_timeout` (60s) | **unchanged (60s)** — keepalive stays unconditional |
| Graceful pod eviction (SIGTERM) | `WorkerEvicted` → re-dispatched | unchanged |
| Event loop fully blocked (no `run_in_thread`) | auto-loop starved → killed at `heartbeat_timeout` | unchanged — still correct, still the only detector for this |
| Streaming work, healthy | beats unconditionally | beats unconditionally; hooks mark progress |
| Wedged-but-looping async (the gap today) | only `start_to_close` (→ 24h under Option 1) | **`TaskStalledError` at `max_no_progress_seconds`** |
| Legit long opaque call, hold declared | keepalive keeps it alive; bounded by `start_to_close` | hold vouches to the declared allowance; then stall fires |
| Legit long opaque call, no hold declared | bounded by `start_to_close` | unbounded hold → 24h backstop; flagged by conformance |
| Wedged blocking call in `run_in_thread`, hold declared | keepalive beats forever → only `start_to_close` | killed at `timeout + budget`; thread orphaned, slot reclaimed |
| `run_best_effort` child-process work (ADR-0017) | parent awaits; keepalive beats | parent's await is inside a hold |

### What authors tune afterward

| Knob | Before | After |
| --- | --- | --- |
| `start_to_close` | the number everyone guesses wrong | 24h backstop, never tuned (with `schedule_to_close` bounding the retry product) |
| `heartbeat_timeout` | "how often do I beat" | **unchanged** — 60s, "nothing is beating" |
| `max_no_progress_seconds` | — | **new**: "max no-progress window", roughly app-independent |
| opaque-operation allowance | none / `start_to_close` | declared once, at the call site, in `holding_progress(timeout=…)` |
| AE node duration | app-set, often *below* the toolkit default | none (see *The Automation Engine layer*) |

## Migration & backward compatibility — "what if I do nothing?"

The first question every consumer asks: *if I don't touch my code, what changes?*

**On upgrade you land in warn mode, and nothing fails differently.** No activity is
killed that wasn't before, `heartbeat_timeout` keeps its meaning and its 60s default,
`start_to_close` keeps its 600s default, and there is no coupled default bump because
no existing knob changes meaning. What you gain is a report about your own code. This
is the main practical advantage over Option 5, which could not be defaulted on at all.

**So there is no migration task on upgrade — only an eventual optimisation.** An app
does nothing, indefinitely, and stays correct: warn mode cannot fail an activity, and
the pre-existing timeouts still apply. Declaring holds is work an app takes on when it
wants the stronger guarantee (a wedge caught in minutes instead of at the backstop), at
a time of its choosing, guided by its own report. Nothing about taking the SDK version
requires touching connector code.

**In enforce mode, whether unchanged code survives depends on its shape.** For the
last two rows, do-nothing code *will* hit stall kills, and that is the design working
as intended — those paths emit nothing the SDK can see, i.e. a healthy-but-quiet task
it cannot distinguish from a wedged one:

| Existing code shape | Warn mode | Enforce mode | Author action |
| --- | --- | --- | --- |
| Streaming through SDK batch writers / stats / `ObjectStore` transfer | silent | Covered — hooks mark progress | none |
| Already calls `context.heartbeat()` | silent | Covered — marks progress | none |
| Opaque blocking call via `run_in_thread` | reported as a long unbounded hold | Covered — unbounded auto-hold; backstop is the only bound | declare an allowance to get a real bound |
| **Custom async loop doing its own I/O, never through an instrumented SDK path** | reported as a no-progress gap | **False-killed** if any gap > budget | add a beat or a hold |
| **Opaque single `await` against the source (connector's own async client)** | reported as a no-progress gap | **False-killed** at the tail | wrap in `holding_progress()` — expected in ~every connector |

**The `start_to_close` backstop lands *with* warn mode, not after it.** This is a
deliberate change of position and the one place this ADR accepts a real regression, so
it deserves the argument in full.

Coupling the backstop to `enforce` would mean warn mode delivers no relief at all:
apps would still be killed at their guessed 2h/6h ceilings while the fleet gathers
data, so every team keeps tuning numbers — precisely the loop FND-165 documents. The
"no upgrade task" property would buy nobody anything.

What coupling it to warn costs: today's small `start_to_close` is *accidentally* the
only thing that kills a wedged activity. Raising it to 24h removes that accident, and
warn mode cannot kill. So a wedge holds a worker slot until the backstop, and the
containment is an alert on the stall metric plus a human, rather than an automatic
kill. That is worse than today for a wedge, and better than today for every
legitimate long run.

Why it is an acceptable trade, and bounded:

- The exposure scales with the **wedge rate**, which is the exact unknown warn mode
  exists to measure. If wedges turn out to be common, the data says so within days and
  enforcement follows; if they are rare — the position argued in the RFC thread — the
  exposure was never real.
- Detection does not regress even though containment does: a wedge is *visible* within
  `max_no_progress_seconds` via the stall metric, where today nothing detects it at all
  (it dies at `start_to_close` by luck, not by diagnosis).
- Slot exhaustion needs roughly a hundred concurrent wedges on one worker. That is a
  function of the same rate, and the metric is the early warning.

The alert on the stall metric is therefore **not optional** while the fleet is in warn
— it is what stands in for the kill. The global default change and retiring the flag
still come at the very end.

**Flipping to `enforce` must be verified against a large-tenant / tail profile, not a
smoke test.** A small tenant's fast steps hide the very gaps that only open at the
tail — the same tail that produced the original failures. Flipping on the strength of a
small run would just relocate the tail failure, and because stall kills retry, an
under-instrumented app would burn the same wasted work up to three times before failing.
Warn mode is what makes this checkable rather than aspirational.

## Rollout

1. **Fix the loop-starvation failures first.** Heartbeat timeout is today's
   dominant alert (Problem 2) and it is an ADR-0010 adoption gap, not a semantics
   gap. Audit and fix the blocking-on-the-loop sites before adding a second
   mechanism on top. This is sequencing, not scope creep: the warn-mode signal in
   step 3 is unreadable while starvation dominates it.
2. **Ship the framework `mark_progress()` hooks** (writers, emission, transfer
   loops) so the streaming majority is covered before anything observes anything.
   Land CNCT-10's timeout-subtype classification alongside, so stall kills are
   distinguishable from starvation kills in the fleet-wide numbers.
3. **Ship `warn` as the default, together with the 24h `start_to_close` backstop — no
   per-app step.** Warn mode cannot fail an activity, so it needs no opt-in: on upgrade
   every app starts producing its own work-list (no-progress gaps, long unbounded
   holds) and contributing to the fleet-wide gap distribution that sets the
   `max_no_progress_seconds` value. The backstop ships here because this is where the
   relief is (see *Migration*), at the accepted 72h retry product (see *Bounding total
   time*). One hard prerequisite of this step: **the stall metric must be alertable**,
   since it stands in for the kill while nothing enforces. `off` ships alongside as an
   env kill-switch.
4. **Ship the conformance rule and the toolkit floor.** Both are independent of the
   `@task` work and both deliver immediately: the floor stops the next app shipping
   a 2h node timeout, and the rule keeps un-held opaque sites visible once teams
   start working through their reports. Pair with the AE ask to drop its own
   default.
5. **Read the fleet data before committing to `enforce` at all.** If long stalls
   turn out to be rare, that is an argument for a generous budget — or for leaving
   the fleet in warn and taking only the AE/backstop half of this ADR. Deciding this
   from measurement rather than from either side's recollection is the point of
   getting warn out early.
6. **Flip per-app to `enforce` when that app wants the guarantee** — its remaining
   gaps inside declared holds or under budget, verified against a **large-tenant /
   tail profile**, not a smoke test. The flag is `progress_watchdog` on `@task` /
   `TaskMetadata`.
7. **Retire the flag and the per-task duration constants** once enforcement is
   universal (or once the warn data justifies enforcing fleet-wide with a generous
   budget, which may make step 6 a formality rather than twenty separate flips).

Per the "delete v3-prep workarounds" discipline, the flag is temporary and its
removal is part of the work, not a follow-up.

## Consequences

**Positive:**
- App authors stop guessing an unguessable duration; `start_to_close` becomes a
  set-and-forget backstop, at both the `@task` and AE layers.
- Wedged-but-alive activities are *detected* in minutes fleet-wide from the first
  release, and *killed* in minutes wherever an app enforces.
- Crash/OOM detection latency is unchanged, and no existing knob changes meaning.
- Relief arrives on upgrade rather than after a per-app migration: the backstop lands
  with warn mode, so legitimately long runs stop dying at a guessed ceiling
  immediately.
- Stalls surface as a typed error naming the last progress signal, which is both
  better for the operator and what makes the rollout measurable.
- The per-app migration is mechanical rather than manual, and it is not on the
  upgrade path at all: warn mode ships on by default, produces the work-list and the
  evidence for each allowance, and declaring holds becomes an optimisation an app
  takes on when it wants the stronger guarantee.
- Whether long stalls actually happen becomes a measured question rather than an
  argued one, fleet-wide, before anything can be killed for it.
- Streaming connectors get correct behavior with no code changes.
- Change is localized to `execution/heartbeat.py` (tracker + loop + hold plumbing),
  the `activities.py` wiring and cancellation branch, one new error leaf, and
  one-line `mark_progress()` calls in existing SDK write/transfer loops.

**Negative:**
- **Wedge containment regresses while the fleet is in warn.** Today's small
  `start_to_close` is accidentally the only thing that kills a wedged activity;
  raising it to a backstop replaces that automatic kill with an alert and a human
  until an app enforces. Accepted deliberately, bounded by measurement — see
  *Migration*. It makes the stall-metric alert mandatory, not optional.
- Opaque single-operation calls need a declared hold to get the stronger guarantee,
  and `holding_progress()` is expected in nearly every connector rather than being a
  rare escape hatch — though this is now optional work an app schedules for itself,
  not a migration gate.
- A third timeout-ish knob exists (`max_no_progress_seconds`), and the docs must be
  clear that it is *not* the beat interval and *not* a duration budget.
- Cancelling the activity task from the watchdog carries the documented asyncio
  protocol caveat.
- Stall kills retry, so a false kill in an under-instrumented app repeats the same
  wasted work up to three times before failing. Warn mode exists to clear that
  category before an app enforces.
- During rollout the flag adds a temporary branch that must be removed.

## What this needs agreement on

Not a list of decisions to make — two stated positions to accept or reject. Everything
else in this ADR follows from them.

1. **We start with a 24h attempt and the existing 3-attempt retry policy — a 72h
   worst case per activity, where today it is 30 minutes.** Detection stays in minutes
   via the stall metric, the product collapses to a single attempt wherever an app
   enforces, and bounding it up front would mean guessing a total duration (see
   *Bounding total time*).
2. **We remove connector time-bounds from AE entirely** — no execution or run timeout
   on connector DAG nodes, which are child-workflow invocations that Temporal requires
   no timeout for. Duration becomes an alert, not a kill. The toolkit floor and the
   conformance rule exist to stop the bound creeping back in per-app (see *The
   Automation Engine layer*). This one needs a cross-team owner to land.

REQ-1609 (checkpointing) is no longer framed as a gate on either of these. Retrying
from zero is what already happens on every `StartToClose` retry and on every manual
re-run, so checkpointing makes the existing and the new paths cheaper without deciding
whether the new path is correct (see *Bounding total time*).

Parameters set by measurement, recorded here so nobody tries to settle them by
argument:

- **`max_no_progress_seconds`.** Starts at **900s** (300s was an earlier proposal;
  FND-165's evidence of apps needing 1800s under the *easier* unconditional regime
  argues for something larger, and a too-tight value is a false-kill generator at the
  tail). The warn-mode gap distribution sets the final value before anything enforces.
- **Per-site hold allowances.** Set from each site's observed p99 — see *Choosing the
  allowance*.
- **Whether long stalls occur at all**, and therefore whether fleet-wide enforcement
  is warranted or a generous budget suffices.

## Related

- **ADR-0010** — Async-First Design and Blocking Code Pitfalls (blocking code owns
  its timeout; `run_in_thread` keeps the loop responsive). This ADR builds directly
  on it, and *Holds* explains why ADR-0010's per-operation timeout cannot be reused
  as a wall-clock bound.
- **ADR-0017** — Native Execution Isolation (`run_best_effort` / `run_fault_isolated`).
- **REQ-1609** — Activity-level checkpointing. Governs how expensive a retry is —
  including, but not only, a stall kill's retry. An efficiency dependency, not a
  correctness one.
- **CNCT-10** — Timeout-error subtype classification. The measurement dependency.
- **FND-165** — The production evidence for both failure modes.
